"""Small numbered migrations; order intents and decisions commit before broker I/O."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from app.models import utcnow


def encode(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    return json.dumps(value, default=lambda x: x.isoformat(), allow_nan=False, sort_keys=True)


TABLES = {'kv', 'sessions', 'candles', 'snapshots', 'candidates', 'decisions', 'orders',
          'fills', 'positions', 'events', 'alerts', 'ai_metrics', 'commands', 'configurations'}
TERMINAL = {'Filled', 'Cancelled', 'ApiCancelled', 'Inactive', 'Rejected'}


class Store:
    def __init__(self, path):
        self.path = Path(path) if str(path) != ':memory:' else Path(':memory:')
        if str(path) != ':memory:':
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.migrate()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.db.close()
    def migrate(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > 1:
            raise RuntimeError('database schema newer than application')
        if version == 0:
            self.db.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE kv(key TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE sessions(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE candles(id INTEGER PRIMARY KEY,symbol TEXT NOT NULL,close_time TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(symbol,close_time));
            CREATE TABLE snapshots(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE candidates(id INTEGER PRIMARY KEY,snapshot_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE decisions(key TEXT PRIMARY KEY,mode TEXT NOT NULL,symbol TEXT NOT NULL,account TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,snapshot TEXT NOT NULL,proposal TEXT,risk TEXT);
            CREATE TABLE orders(ref TEXT PRIMARY KEY REFERENCES decisions(key),status TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE fills(execution_id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE positions(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE alerts(id INTEGER PRIMARY KEY,severity TEXT NOT NULL,message TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
            CREATE TABLE ai_metrics(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE commands(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',created_at TEXT NOT NULL);
            CREATE TABLE configurations(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            PRAGMA user_version=1;
            COMMIT;
            ''')
        if self.kv('control') is None:
            self.set_kv('control', {'mode': 'OBSERVE', 'paused': False, 'kill_switch': False,
                                  'session_id': None, 'live_confirmation_until': None})
    def kv(self, key, default=None):
        row = self.db.execute('SELECT payload FROM kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def _kv(self, key, value):
        self.db.execute('INSERT INTO kv VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at', (key,encode(value),utcnow().isoformat()))
    def set_kv(self, key, value):
        with self.db: self._kv(key,value)
    def rows(self, table, limit=100):
        if table not in TABLES: raise ValueError('unknown table')
        return [dict(r) for r in self.db.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?', (min(max(int(limit),1),10000),))]
    def event(self, kind, payload, now=None):
        with self.db:
            self.db.execute('INSERT INTO events(kind,payload,created_at) VALUES(?,?,?)',(kind,encode(payload),(now or utcnow()).isoformat()))
    def alert(self, message, severity='critical'):
        with self.db:
            if not self.db.execute('SELECT 1 FROM alerts WHERE message=? AND resolved=0',(message,)).fetchone():
                self.db.execute('INSERT INTO alerts(severity,message,created_at) VALUES(?,?,?)',(severity,message,utcnow().isoformat()))
    def resolve(self, message):
        with self.db: self.db.execute('UPDATE alerts SET resolved=1 WHERE message=?',(message,))
    def critical(self):
        return [r[0] for r in self.db.execute("SELECT message FROM alerts WHERE severity='critical' AND resolved=0")]
    def get_control(self): return self.kv('control')
    def control(self, action, payload=None):
        allowed={'pause','resume','mode','approve','reject','cancel','disarm','kill','clear_kill','confirm_live','config'}
        if action not in allowed: raise ValueError('unsupported control')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            c = self.get_control()
            if action in {'kill','pause','disarm'}:
                if action=='kill': c['kill_switch']=True
                if action in {'kill','pause'}: c['paused']=True
                if action=='disarm': c['mode']='OBSERVE'
                c['live_confirmation_until']=None
                self._kv('control',c)
            cur=self.db.execute('INSERT INTO commands(kind,payload,created_at) VALUES(?,?,?)',(action,encode(payload or {}),utcnow().isoformat()))
        return cur.lastrowid
    def claim_commands(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows=[dict(r) for r in self.db.execute("SELECT * FROM commands WHERE status='queued' ORDER BY id")]
            self.db.executemany("UPDATE commands SET status='processing' WHERE id=?",[(r['id'],) for r in rows])
        return rows
    def command_done(self, id, status='done'):
        with self.db: self.db.execute('UPDATE commands SET status=? WHERE id=?',(status,id))
    def save_bar(self, bar):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO candles(symbol,close_time,payload,created_at) VALUES(?,?,?,?)', (bar.symbol,bar.close_time.isoformat(),encode(bar),bar.close_time.isoformat()))
    def bars(self, symbol, limit=10):
        from app.models import Bar
        return [Bar.model_validate_json(r[0]) for r in reversed(self.db.execute('SELECT payload FROM candles WHERE symbol=? ORDER BY close_time DESC LIMIT ?', (symbol,limit)).fetchall())]
    def begin_decision(self, key, mode, account, snapshot):
        now=snapshot.timestamp.isoformat()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            cursor=self.db.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',(key,mode,snapshot.symbol,account,now,'evaluating',encode(snapshot),None,None))
            if not cursor.rowcount: return False
            self.db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?)',(snapshot.snapshot_id,encode(snapshot),now))
            self.db.executemany('INSERT INTO candidates(snapshot_id,payload,created_at) VALUES(?,?,?)',[(snapshot.snapshot_id,encode(c),now) for c in snapshot.candidates])
        return True
    def decision(self, key):
        row=self.db.execute('SELECT * FROM decisions WHERE key=?',(key,)).fetchone()
        return dict(row) if row else None
    def finish_decision(self, key, proposal, risk, status):
        with self.db:
            self.db.execute('UPDATE decisions SET proposal=?,risk=?,status=? WHERE key=?',(encode(proposal),encode(risk),status,key))
    def prepare(self, key, intent, risk):
        # One transaction commits the final risk result and immutable order intent.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            c=self.get_control()
            if c['mode'] != intent.mode or c['paused'] or (c['kill_switch'] and intent.intent!='SELL_TO_CLOSE'):
                raise RuntimeError('controls changed before send')
            row=self.decision(key)
            if not row or row['status'] not in {'ready','awaiting_approval'}:
                raise RuntimeError('decision already processed')
            self.db.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(key,'PREPARED',encode({'intent':intent.model_dump(mode='json'),'broker_order':None}),intent.created_at.isoformat(),intent.created_at.isoformat()))
            self.db.execute("UPDATE decisions SET status='prepared',risk=? WHERE key=?",(encode(risk),key))
    def order(self, ref):
        r=self.db.execute('SELECT * FROM orders WHERE ref=?',(ref,)).fetchone()
        return dict(r) if r else None
    def update_order(self, ref, status, broker_order=None):
        with self.db:
            row=self.order(ref)
            if row:
                payload=json.loads(row['payload'])
                if broker_order is not None: payload['broker_order']=broker_order.model_dump(mode='json')
                # Never regress a complete fill on a late cancellation/status callback.
                if row['status']=='Filled': status='Filled'
                self.db.execute('UPDATE orders SET status=?,payload=?,updated_at=? WHERE ref=?',(status,encode(payload),utcnow().isoformat(),ref))
                self.db.execute('UPDATE decisions SET status=? WHERE key=?',(status,ref))
    def save_state(self, state):
        with self.db:
            for f in state.fills:
                self.db.execute('INSERT INTO fills VALUES(?,?,?) ON CONFLICT(execution_id) DO UPDATE SET payload=excluded.payload',(f.execution_id,encode(f),f.timestamp.isoformat()))
            self.db.execute('INSERT INTO positions(payload,created_at) VALUES(?,?)',(encode(state.positions),state.account.timestamp.isoformat()))
            self._kv('broker_state',state.model_dump(mode='json'))
    def opening_stats(self, account, now):
        rows=self.db.execute('SELECT o.payload,o.created_at FROM orders o JOIN decisions d ON d.key=o.ref WHERE d.account=?',(account,)).fetchall()
        # Conservatively count every durable opening intent, including rejected/uncertain sends.
        from zoneinfo import ZoneInfo
        market_tz=ZoneInfo('America/New_York')
        day=now.astimezone(market_tz).date()
        openings=[(json.loads(r[0])['intent'],datetime.fromisoformat(r[1])) for r in rows]
        openings=[(x,t) for x,t in openings if x['intent']!='SELL_TO_CLOSE']
        count=sum(t.astimezone(market_tz).date()==day for _,t in openings)
        last=max((t for _,t in openings),default=None)
        return count,last
    def pending_intents(self):
        rows=self.db.execute('SELECT payload,status FROM orders').fetchall()
        return [json.loads(r[0])['intent'] for r in rows if r[1] not in TERMINAL]
