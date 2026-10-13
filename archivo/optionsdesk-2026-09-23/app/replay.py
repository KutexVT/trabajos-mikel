"""JSONL replay uses a virtual clock and the same coordinator/risk/executor as runtime."""
from __future__ import annotations
import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from pydantic import Field, AwareDatetime
from app.models import Model, Candidate, Bar
from app.config import Config
from app.broker import FakeBroker
from app.analyst import ScriptedAnalyst
from app.engine import Engine
from app.store import Store


class Frame(Model):
    timestamp: AwareDatetime
    bar: Bar | None = None
    tick: dict | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    event: str | None = None
    proposal: dict | None = None
    daily_pnl: float | None = None
    fill_quantity: float | None = Field(default=None,gt=0,le=1)
    auto_fill: bool = True


async def replay(path, db_path=None):
    path=Path(path)
    lines=path.read_text().splitlines()
    frames=[Frame.model_validate_json(line) for line in lines if line.strip()]
    if not frames: raise ValueError('empty replay')
    if any(b.timestamp<a.timestamp for a,b in zip(frames,frames[1:])): raise ValueError('replay must be chronological')
    if db_path and Path(db_path).exists(): raise ValueError('replay requires a new database; never overwrites existing audit')
    temporary=tempfile.TemporaryDirectory(prefix='replay-',dir=Path(__file__).resolve().parent.parent/'work') if db_path is None else None
    target=Path(temporary.name)/'replay.sqlite3' if temporary else Path(db_path)
    config=Config(mode='PAPER_AUTO',symbols=sorted({f.bar.symbol for f in frames if f.bar} | {c.symbol for f in frames for c in f.candidates}),fake_latency_ms=0)
    now=frames[0].timestamp
    broker=FakeBroker(config)
    broker.set_market(frames[0].candidates,now)
    analyst=ScriptedAnalyst(config)
    store=Store(target)
    engine=Engine(config,store,broker,analyst,clock=lambda:now)
    await engine.start(restore_fake=False)
    try:
        for frame in frames:
            now=frame.timestamp
            broker.auto_fill=frame.auto_fill
            broker.set_market(frame.candidates,now)
            if frame.daily_pnl is not None: broker.account.daily_pnl=frame.daily_pnl
            if frame.event=='disconnect': await broker.disconnect()
            elif frame.event=='reconnect': await broker.connect()
            elif frame.event=='lost_ack': broker.fail_after_submit=True
            elif frame.event=='reject': broker.reject_next=True
            elif frame.event=='partial_fill':
                pending=[o for o in broker.orders.values() if o.status=='Submitted']
                if pending: broker.partial_fill(pending[-1].order_id,frame.fill_quantity or .5)
            elif frame.event=='cancel':
                pending=[o for o in broker.orders.values() if o.status=='Submitted']
                if pending: await broker.cancel(pending[-1].order_id)
            elif frame.event=='kill':
                store.control('kill'); await engine.controls()
            if frame.tick:
                for bar in engine.candles.feed(frame.tick): await engine.on_bar(bar,frame.proposal)
            if frame.bar:
                if frame.bar.close_time>now: raise ValueError('replay look-ahead bar')
                await engine.on_bar(frame.bar,frame.proposal)
                if frame.event=='duplicate': await engine.on_bar(frame.bar,frame.proposal)
            await engine.reconcile()
        # Stable business results exclude process/session IDs and audit wall-clock metadata.
        decisions=[{'key':d['key'],'status':d['status'],'action':json.loads(d['proposal'])['action'],
                    'risk':json.loads(d['risk'])['reasons']} for d in reversed(store.rows('decisions',10000))]
        state=await broker.state()
        report={'simulated':True,'fixture':path.name,'decisions':decisions,
                'orders':[o.model_dump(mode='json') for o in state.orders],
                'fills':[f.model_dump(mode='json') for f in state.fills],
                'positions':[p.model_dump(mode='json') for p in state.positions],
                'cash':round(state.account.cash,8),'realized_pnl':round(state.account.realized_pnl,8),
                'unrealized_pnl':round(state.account.unrealized_pnl,8),'critical_alerts':store.critical()}
        report['result_sha256']=hashlib.sha256(json.dumps(report,sort_keys=True,allow_nan=False).encode()).hexdigest()
        return report
    finally:
        await engine.stop()
        store.close()
        if temporary: temporary.cleanup()
