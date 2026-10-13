"""One local coordinator. Durable decisions, fresh risk checks, then broker I/O."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from app.config import Config
from app.models import BrokerState, Proposal, Snapshot, OrderIntent, RiskResult, hold, utcnow
from app.store import Store, TERMINAL, encode
from app.risk import evaluate
from app.market import CandleBuilder, select_candidates, make_snapshot


def decision_key(account, mode, symbol, close_time, strategy):
    text='|'.join([account,mode,symbol,close_time.isoformat(),strategy])
    return hashlib.sha256(text.encode()).hexdigest()


class Engine:
    def __init__(self, config, store, broker, analyst, clock=utcnow):
        self.config=config
        self.store=store
        self.broker=broker
        self.analyst=analyst
        self.clock=clock
        self.session_id=uuid.uuid4().hex
        self.state=None
        self.reconciled=False
        self.candles=CandleBuilder()
        self._state_lock=asyncio.Lock()
        self._execute_lock=asyncio.Lock()
        self._approved=set()
        self._tasks=set()
        self._active_symbols=set()
        self._running=False
        self.broker.pre_submit=self.pre_submit
    async def start(self, restore_fake=True):
        self.store.set_kv('config',self.config.model_dump(mode='json'))
        c=self.store.get_control()
        c.update(session_id=self.session_id,live_confirmation_until=None,confirmed_session=None,confirmed_account=None,
                 mode=self.config.mode if not self.config.mode.startswith('LIVE') else 'OBSERVE')
        self.store.set_kv('control',c)
        with self.store.db:
            self.store.db.execute('INSERT INTO sessions VALUES(?,?,?)',(self.session_id,encode({'broker':type(self.broker).__name__}),self.clock().isoformat()))
            self.store.db.execute("UPDATE decisions SET status='interrupted' WHERE status IN ('evaluating','ready','awaiting_approval')")
            self.store.db.execute("UPDATE commands SET status='interrupted' WHERE status IN ('processing','queued')")
        saved=self.store.kv('broker_state')
        if restore_fake and saved and saved['account']['environment']=='fake' and hasattr(self.broker,'restore'):
            self.broker.restore(BrokerState.model_validate(saved))
        try:
            await self.broker.connect()
            await self.reconcile()
        except Exception as exc:
            self.fault('connection',exc)
        self.publish()
    def fault(self, stage, exc):
        self.reconciled=False
        self.store.alert('broker_unavailable')
        self.store.event('error',{'stage':stage,'type':type(exc).__name__},self.clock())
        self.publish()
    def publish(self, **extra):
        status=self.store.kv('status',{})
        c=self.store.get_control()
        status.update(mode=c['mode'],paused=c['paused'],kill_switch=c['kill_switch'],session_id=self.session_id,
                      broker=type(self.broker).__name__,connected=bool(self.state and self.state.connected and self.reconciled),
                      risk_healthy=self.reconciled and not self.store.critical(),critical_alerts=self.store.critical(),
                      account=self.state.account.model_dump(mode='json') if self.state else None,
                      updated_at=self.clock().isoformat(),pid=os.getpid())
        status.update(extra)
        self.store.set_kv('status',status)
    async def reconcile(self):
        async with self._state_lock:
            state=await self.broker.state()
            state=BrokerState.model_validate(state.model_dump(mode='json'))
            self.state=state
            if not state.connected or not state.complete:
                self.reconciled=False
                self.store.alert('broker_unavailable')
                self.publish()
                return False
            if any(o.account_id and o.account_id!=state.account.account_id for o in state.orders):
                self.store.alert('account_order_mismatch')
            for alert in state.critical_alerts: self.store.alert('broker:'+alert)
            by_ref={o.ref:o for o in state.orders if o.ref}
            unknown=[]
            for row in self.store.rows('orders',10000):
                intent=json.loads(row['payload'])['intent']
                if intent['account_id']!=state.account.account_id:
                    unknown.append(row['ref'])
                    continue
                order=by_ref.get(row['ref'])
                if order:
                    if order.conid!=intent['candidate']['conid'] or order.intent!=intent['intent'] or order.quantity!=intent['quantity']:
                        self.store.alert('order_identity_mismatch:'+row['ref'])
                    self.store.update_order(row['ref'],order.status,order)
                    self.store.resolve('uncertain_order:'+row['ref'])
                elif row['status'] not in TERMINAL:
                    fills=[f for f in state.fills if f.ref==row['ref']]
                    if sum(f.quantity for f in fills)>=intent['quantity']:
                        self.store.update_order(row['ref'],'Filled')
                        self.store.resolve('uncertain_order:'+row['ref'])
                    else: unknown.append(row['ref'])
            for ref in unknown: self.store.alert('uncertain_order:'+ref)
            self.store.save_state(state)
            self.store.resolve('broker_unavailable')
            self.reconciled=not unknown and not self.store.critical()
            self.publish()
            return self.reconciled
    def risk(self, proposal, snapshot, *, exclude_ref=None):
        if not self.state:
            return RiskResult(allowed=False,reasons=['broker_unavailable'])
        count,last=self.store.opening_stats(self.state.account.account_id,self.clock())
        pending=[x for x in self.store.pending_intents() if x['ref']!=exclude_ref]
        return evaluate(proposal,snapshot,self.state,self.config,self.store.get_control(),self.clock(),
                        reconciled=self.reconciled,critical=self.store.critical(),opening_count=count,last_opening=last,pending=pending)
    async def on_bar(self, bar, proposal=None):
        self.store.save_bar(bar)
        self.publish(last_data=bar.close_time.isoformat(),next_evaluation=(bar.close_time+timedelta(minutes=self.config.decision_minutes-bar.close_time.minute%self.config.decision_minutes)).isoformat())
        try: await self.reconcile()
        except Exception as exc: self.fault('bar_state',exc)
        if int(bar.close_time.timestamp()//60)%self.config.decision_minutes: return None
        candidates=[]
        try:
            candidates=select_candidates(await self.broker.discover(bar.symbol,self.clock()),self.config,self.clock())
        except Exception as exc:
            self.store.event('market_error',{'type':type(exc).__name__},self.clock())
        snapshot=make_snapshot(bar.symbol,bar,self.store.bars(bar.symbol,12),candidates,self.state.positions if self.state else [],self.config)
        snapshot.timestamp=self.clock()
        return await self.decide(snapshot,proposal)
    async def decide(self, snapshot, proposal=None):
        mode=self.store.get_control()['mode']
        account=self.state.account.account_id if self.state else 'unavailable'
        key=decision_key(account,mode,snapshot.symbol,snapshot.bar.close_time,self.config.strategy)
        if not self.store.begin_decision(key,mode,account,snapshot):
            self.store.event('duplicate_decision',{'key':key},self.clock())
            return key
        signature=encode([snapshot.signal,[(p.conid,p.quantity) for p in snapshot.positions]])
        if proposal is None:
            if snapshot.signal=='NONE' or not snapshot.candidates or self.store.kv('signal:'+snapshot.symbol)==signature:
                proposal=hold(self.clock(),'No new actionable signal')
            elif not self.reconciled or self.store.get_control()['paused'] or self.store.get_control()['kill_switch']:
                proposal=hold(self.clock(),'Execution is blocked')
            else:
                try: proposal=await self.analyst.analyze(snapshot)
                except Exception as exc: proposal=hold(self.clock(),'Analyst failure: '+type(exc).__name__)
                metric=getattr(self.analyst,'last_metric',{})
                with self.store.db:
                    self.store.db.execute('INSERT INTO ai_metrics(payload,created_at) VALUES(?,?)',(encode(metric),self.clock().isoformat()))
        try:
            proposal=Proposal.model_validate_json(proposal) if isinstance(proposal,str) else Proposal.model_validate(proposal.model_dump(mode='json') if isinstance(proposal,Proposal) else proposal)
        except Exception:
            proposal=hold(self.clock(),'Invalid structured proposal')
        self.store.set_kv('signal:'+snapshot.symbol,signature)
        # Broker/account may have changed while the analyst was running.
        try: await self.reconcile()
        except Exception as exc: self.fault('decision_state',exc)
        risk=self.risk(proposal,snapshot)
        status='hold' if proposal.action=='HOLD' else 'rejected'
        if risk.allowed: status='awaiting_approval' if mode.endswith('APPROVAL') else 'ready'
        self.store.finish_decision(key,proposal,risk,status)
        self.publish(last_decision={'key':key,'action':proposal.action,'status':status,'reason':proposal.reason,'risk':risk.reasons})
        if status=='ready': await self.execute(key)
        return key
    def pre_submit(self, intent):
        """Broker calls this at the final synchronous socket/mutation boundary."""
        intent=OrderIntent.model_validate(intent.model_dump(mode='json'))
        c=self.store.get_control()
        row=self.store.order(intent.ref)
        if not row or row['status']!='PREPARED': raise RuntimeError('No fresh durable intent')
        saved=OrderIntent.model_validate(json.loads(row['payload'])['intent'])
        if saved!=intent: raise RuntimeError('Intent differs from audit')
        if c['mode']=='OBSERVE' or c['mode']!=intent.mode or c['paused'] or (c['kill_switch'] and intent.intent!='SELL_TO_CLOSE'):
            raise RuntimeError('Execution disarmed')
        if c['mode'].endswith('APPROVAL') and intent.ref not in self._approved:
            raise RuntimeError('Human approval missing')
        if not self.reconciled or self.store.critical() or not self.state or not self.state.connected:
            raise RuntimeError('Risk engine not healthy')
        if (self.clock()-intent.candidate.timestamp).total_seconds()>self.config.max_data_age_seconds:
            raise RuntimeError('Quote expired before send')
        if intent.account_id!=self.state.account.account_id:
            raise RuntimeError('Account changed')
        if c['mode'].startswith('LIVE'):
            expiry=c.get('live_confirmation_until')
            if (os.environ.get('IBKR_ENV')!='live' or os.environ.get('LIVE_TRADING_ENABLED','false').lower()!='true'
                or self.state.account.environment!='live' or c.get('confirmed_session')!=self.session_id
                or c.get('confirmed_account')!=intent.account_id or not expiry or datetime.fromisoformat(expiry)<=self.clock()):
                raise RuntimeError('Live is locked')
        elif self.state.account.environment not in {'paper','fake'} or os.environ.get('IBKR_ENV','paper')!='paper':
            raise RuntimeError('Paper account mismatch')
    async def execute(self, key, approved=False):
        async with self._execute_lock:
            row=self.store.decision(key)
            if not row or row['status'] not in {'ready','awaiting_approval'}: return False
            if row['status']=='awaiting_approval' and not approved: return False
            proposal=Proposal.model_validate_json(row['proposal'])
            snapshot=Snapshot.model_validate_json(row['snapshot'])
            if row['mode']!=self.store.get_control()['mode']:
                self.store.finish_decision(key,proposal,RiskResult(allowed=False,reasons=['mode_changed']),'rejected')
                return False
            try:
                await self.reconcile()
                original=next(c for c in snapshot.candidates if c.candidate_id==proposal.candidate_id)
                fresh=await self.broker.refresh(original,self.clock())
                identity=('conid','symbol','right','expiry','strike','multiplier','exchange','currency')
                if any(getattr(original,x)!=getattr(fresh,x) for x in identity): raise ValueError('Contract identity changed')
                for field in ('bid','ask','mid'):
                    old,new=getattr(original,field),getattr(fresh,field)
                    if old is None or new is None or old<=0 or abs(new-old)/old>self.config.max_quote_change_pct:
                        raise ValueError('Quote materially changed')
                fresh.candidate_id=original.candidate_id
                snapshot.candidates=[fresh if c.candidate_id==original.candidate_id else c for c in snapshot.candidates]
                risk=self.risk(proposal,snapshot)
                if not risk.allowed:
                    self.store.finish_decision(key,proposal,risk,'rejected')
                    return False
                intent=OrderIntent(ref=key,account_id=self.state.account.account_id,mode=row['mode'],candidate=fresh,intent=proposal.intent,
                                   quantity=proposal.quantity,limit_price=proposal.limit_price,created_at=self.clock())
                self.store.prepare(key,intent,risk)
                if approved: self._approved.add(key)
                order=await self.broker.submit(intent)
                self.store.update_order(key,order.status,order)
                await self.reconcile()
                self.store.event('order_submitted',{'key':key,'order_id':order.order_id},self.clock())
                return True
            except Exception as exc:
                if self.store.order(key):
                    # Ambiguous I/O outcome: never resend. Only reconciliation can resolve it.
                    self.store.update_order(key,'UNKNOWN')
                    self.store.alert('uncertain_order:'+key)
                    self.reconciled=False
                    try: await self.reconcile()
                    except Exception: pass
                else:
                    self.store.finish_decision(key,proposal,RiskResult(allowed=False,reasons=['preflight_failed:'+type(exc).__name__]),'rejected')
                self.store.event('execution_error',{'key':key,'type':type(exc).__name__},self.clock())
                self.publish()
                return False
            finally: self._approved.discard(key)
    async def controls(self):
        for command in self.store.claim_commands():
            action=command['kind']; payload=json.loads(command['payload'])
            try:
                c=self.store.get_control()
                if action in {'kill','pause','disarm'}:
                    # Kill also asks the broker to cancel outstanding opening orders.
                    if action=='kill' and self.state:
                        for order in self.state.orders:
                            if order.intent!='SELL_TO_CLOSE' and order.status not in TERMINAL:
                                await self.broker.cancel(order.order_id)
                    self.store.event('mode_change',{'action':action,'control':c},self.clock())
                elif action=='resume':
                    if c['kill_switch']: raise ValueError('reset kill switch first')
                    c['paused']=False; self.store.set_kv('control',c)
                elif action=='clear_kill':
                    if not payload.get('confirmed'): raise ValueError('explicit reset required')
                    c['kill_switch']=False; c['paused']=True; self.store.set_kv('control',c)
                elif action=='mode':
                    mode=Config(mode=payload['mode']).mode
                    if mode.startswith('LIVE'):
                        expiry=c.get('live_confirmation_until')
                        if not self.state or self.state.account.environment!='live' or os.environ.get('IBKR_ENV')!='live' or os.environ.get('LIVE_TRADING_ENABLED','false').lower()!='true' or not expiry or datetime.fromisoformat(expiry)<=self.clock() or c.get('confirmed_session')!=self.session_id or c.get('confirmed_account')!=self.state.account.account_id or not self.reconciled or self.store.critical():
                            raise ValueError('Live gates not satisfied')
                    c['mode']=mode; self.store.set_kv('control',c)
                    self.store.event('mode_change',{'mode':mode},self.clock())
                elif action=='confirm_live':
                    if not self.state or payload.get('session_id')!=self.session_id or payload.get('phrase')!='ENABLE LIVE '+self.state.account.account_id or not payload.get('ui_session_id'):
                        raise ValueError('invalid session confirmation')
                    if self.state.account.environment!='live' or os.environ.get('IBKR_ENV')!='live' or os.environ.get('LIVE_TRADING_ENABLED','false').lower()!='true' or not self.reconciled or self.store.critical():
                        raise ValueError('Live gates not satisfied')
                    c.update(confirmed_session=self.session_id,confirmed_account=self.state.account.account_id,live_confirmation_until=(self.clock()+timedelta(minutes=5)).isoformat())
                    self.store.set_kv('control',c)
                elif action=='config':
                    new=Config.model_validate(payload)
                    # Operational mode has a separate guarded control path.
                    if new.mode!=self.config.mode: raise ValueError('use mode control')
                    self.config=new
                    self.broker.config=new
                    if hasattr(self.analyst,'config'): self.analyst.config=new
                    self.store.set_kv('config',new.model_dump(mode='json'))
                    with self.store.db:
                        self.store.db.execute('INSERT INTO configurations(payload,created_at) VALUES(?,?)',(encode(new),self.clock().isoformat()))
                elif action=='approve': await self.execute(payload['key'],approved=True)
                elif action=='reject':
                    row=self.store.decision(payload['key'])
                    if row and row['status']=='awaiting_approval':
                        self.store.finish_decision(row['key'],json.loads(row['proposal']),RiskResult(allowed=False,reasons=['human_rejection']),'rejected')
                elif action=='cancel':
                    row=self.store.order(payload['ref'])
                    if not row or row['status'] in TERMINAL: raise ValueError('order not pending')
                    order=json.loads(row['payload'])['broker_order']
                    if not order: raise ValueError('unknown broker order; reconcile first')
                    await self.broker.cancel(order['order_id'])
                    await self.reconcile()
                self.store.command_done(command['id'])
            except Exception as exc:
                self.store.command_done(command['id'],'rejected')
                self.store.event('control_rejected',{'action':action,'type':type(exc).__name__},self.clock())
            self.publish()
    def _schedule_bar(self, bar):
        if bar.symbol in self._active_symbols:
            self.store.save_bar(bar)
            self.store.event('busy_cycle_hold',{'symbol':bar.symbol},self.clock())
            return
        self._active_symbols.add(bar.symbol)
        async def work():
            try: await self.on_bar(bar)
            except Exception as exc: self.fault('bar',exc)
            finally: self._active_symbols.discard(bar.symbol)
        task=asyncio.create_task(work())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
    async def run(self, max_seconds=None):
        await self.start()
        self._running=True
        start=time.monotonic(); last_reconnect=0
        try:
            while self._running and (max_seconds is None or time.monotonic()-start<max_seconds):
                await self.controls()
                if not self.state or not self.state.connected:
                    if time.monotonic()-last_reconnect>5:
                        last_reconnect=time.monotonic()
                        try:
                            await self.broker.connect(); await self.reconcile()
                        except Exception as exc: self.fault('reconnect',exc)
                    await asyncio.sleep(.25)
                    continue
                try:
                    event=await self.broker.next_event(timeout=.25)
                    if event:
                        if event['kind']=='tick':
                            for bar in self.candles.feed(event): self._schedule_bar(bar)
                            self.publish(last_data=event['timestamp'].isoformat() if hasattr(event['timestamp'],'isoformat') else event['timestamp'])
                        elif event['kind']=='bar':
                            self._schedule_bar(event['bar'])
                        else: await self.reconcile()
                    for bar in self.candles.flush(self.clock()): self._schedule_bar(bar)
                    self.publish()
                except Exception as exc:
                    self.fault('events',exc)
                    if self.state: self.state.connected=False
        finally: await self.stop()
    async def stop(self):
        self._running=False
        for task in list(self._tasks): task.cancel()
        if self._tasks: await asyncio.gather(*self._tasks,return_exceptions=True)
        try: await self.reconcile()
        except Exception: pass
        await self.broker.disconnect()
        if hasattr(self.analyst,'close'): await self.analyst.close()
        self.reconciled=False
        c=self.store.get_control()
        c.update(mode='OBSERVE',live_confirmation_until=None,confirmed_session=None)
        self.store.set_kv('control',c)
        self.publish(stopped=True)
