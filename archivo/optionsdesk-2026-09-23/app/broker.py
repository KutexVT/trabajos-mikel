from __future__ import annotations

import asyncio
import hashlib
import math
from datetime import datetime, timedelta
from typing import Callable, Protocol

from app.config import Config
from app.models import Account, BrokerOrder, BrokerState, Candidate, Fill, OrderIntent, Position, utcnow

TERMINAL = {'Filled', 'Cancelled', 'ApiCancelled', 'Inactive', 'Rejected'}


class Broker(Protocol):
    pre_submit: Callable[[OrderIntent], None] | None
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def state(self) -> BrokerState: ...
    async def discover(self, symbol: str, now: datetime) -> list[Candidate]: ...
    async def refresh(self, candidate: Candidate, now: datetime) -> Candidate: ...
    async def submit(self, intent: OrderIntent) -> BrokerOrder: ...
    async def cancel(self, order_id: str) -> None: ...
    async def next_event(self, timeout: float = 1) -> dict | None: ...


def submission_guard(intent: OrderIntent, state: BrokerState, config: Config, now: datetime) -> None:
    """Broker-side last fence; the engine's callback adds persisted controls."""
    OrderIntent.model_validate(intent.model_dump())
    if not state.connected or not state.complete or state.critical_alerts:
        raise ValueError('broker unavailable or unreconciled')
    account, candidate = state.account, intent.candidate
    expected = 'live' if intent.mode.startswith('LIVE_') else 'paper'
    actual = 'paper' if account.environment == 'fake' else account.environment
    if intent.mode == 'OBSERVE' or actual != expected or intent.account_id != account.account_id:
        raise ValueError('account or mode mismatch')
    if candidate.symbol not in config.symbols or candidate.currency != 'USD' or candidate.multiplier != 100:
        raise ValueError('unsupported contract')
    if not config.min_dte <= (candidate.expiry - now.date()).days <= config.max_dte:
        raise ValueError('expiry outside permitted range')
    if candidate.data_type not in ({'simulated'} if account.environment == 'fake' else {'live'}):
        raise ValueError('market data not live')
    if not 0 <= (now - candidate.timestamp).total_seconds() <= config.max_data_age_seconds:
        raise ValueError('stale quote')
    if candidate.bid is None or candidate.ask is None or not 0 < candidate.bid <= candidate.ask:
        raise ValueError('missing or invalid quote')
    if (candidate.ask - candidate.bid) / ((candidate.ask + candidate.bid) / 2) > config.max_spread_pct:
        raise ValueError('spread exceeds limit')
    if intent.intent == 'SELL_TO_CLOSE':
        quantity = sum(p.quantity for p in state.positions if p.conid == candidate.conid)
        reserved = sum(o.quantity - o.filled for o in state.orders if o.conid == candidate.conid and o.intent == 'SELL_TO_CLOSE' and o.status not in TERMINAL)
        if quantity - reserved < intent.quantity:
            raise ValueError('short options forbidden')
    else:
        if (intent.intent == 'BUY_TO_OPEN_CALL') != (candidate.right == 'CALL'):
            raise ValueError('option right mismatch')
        cost = intent.limit_price * candidate.multiplier * intent.quantity + config.commission_per_contract
        if cost > account.cash or cost > config.risk_capital * config.max_trade_risk_pct:
            raise ValueError('insufficient cash or excessive trade risk')
        if account.daily_pnl <= -config.risk_capital * config.max_daily_loss_pct:
            raise ValueError('daily loss limit')
        if any(p.symbol == candidate.symbol and p.quantity != 0 for p in state.positions):
            raise ValueError('position already open for symbol')
    if any(o.conid == candidate.conid and o.intent == intent.intent and o.status not in TERMINAL for o in state.orders):
        raise ValueError('equivalent order pending')


class FakeBroker:
    """Deterministic paper exchange. Synthetic conids never enter IBKRBroker."""
    def __init__(self, config: Config):
        self.config = config
        self.pre_submit: Callable[[OrderIntent], None] | None = None
        self.connected = False
        self.now = utcnow()
        self.manual_clock = False
        self.account = Account(account_id='FAKE-PAPER', environment='fake', cash=config.risk_capital,
                               net_liquidation=config.risk_capital, daily_pnl=0, timestamp=self.now)
        self.positions: dict[int, Position] = {}
        self.orders: dict[str, BrokerOrder] = {}
        self.fills: dict[str, Fill] = {}
        self.candidates: dict[str, Candidate] = {}
        self.critical_alerts: list[str] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.auto_fill = True
        self.reject_next = False
        self.fail_after_submit = False
        self._counter = 0
        self._tick = 0
        self._last_tick = 0.0
        self._initial_equity = config.risk_capital

    async def _latency(self):
        await asyncio.sleep(self.config.fake_latency_ms / 1000)

    async def connect(self):
        await self._latency()
        self.connected = True
        self.events.put_nowait({'kind': 'state'})

    async def disconnect(self):
        self.connected = False
        self.events.put_nowait({'kind': 'state'})

    def _state(self):
        return BrokerState(connected=self.connected, account=self.account.model_copy(deep=True),
                           positions=[p.model_copy(deep=True) for p in self.positions.values() if p.quantity],
                           orders=[o.model_copy(deep=True) for o in self.orders.values()],
                           fills=[f.model_copy(deep=True) for f in self.fills.values()],
                           complete=self.connected, critical_alerts=list(self.critical_alerts))

    async def state(self):
        if not self.manual_clock:
            self.now = utcnow()
            self.account.timestamp = self.now
        return self._state()

    def restore(self, state: BrokerState):
        """Restore only simulated state after engine persistence, never an IB account."""
        if state.account.environment != 'fake' or state.account.account_id != 'FAKE-PAPER':
            raise ValueError('cannot restore external account into fake broker')
        self.account = state.account.model_copy(deep=True)
        self.positions = {p.conid: p.model_copy(deep=True) for p in state.positions}
        self.orders = {o.order_id: o.model_copy(deep=True) for o in state.orders}
        self.fills = {f.execution_id: f.model_copy(deep=True) for f in state.fills}
        self._counter = max((int(x.removeprefix('F')) for x in self.orders), default=0)
        self.now = self.account.timestamp

    def set_market(self, candidates: list[Candidate], now: datetime):
        self.now, self.manual_clock = now, True
        self.account.timestamp = now
        self.candidates = {c.candidate_id: c.model_copy(deep=True) for c in candidates}
        self._mark()
        if self.auto_fill:
            for order in list(self.orders.values()):
                self._try_fill(order)

    async def discover(self, symbol: str, now: datetime):
        if not self.connected or symbol not in self.config.symbols:
            return []
        if self.manual_clock:
            return [c.model_copy(deep=True) for c in self.candidates.values() if c.symbol == symbol]
        expiry = now.date() + timedelta(days=max(30, self.config.min_dte))
        for right in ('CALL', 'PUT'):
            key = f'SIM-{symbol}-{expiry}-{right}-100'
            conid = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], 'big') or 1
            candidate = Candidate(candidate_id=key, conid=conid, symbol=symbol, right=right, expiry=expiry,
                                  strike=100, multiplier=100, exchange='SIM', bid=.34, ask=.36, mid=.35,
                                  last=.35, volume=1000, open_interest=5000, delta=.5 if right == 'CALL' else -.5,
                                  timestamp=now, data_type='simulated')
            self.candidates[key] = candidate
        return [c.model_copy(deep=True) for c in self.candidates.values() if c.symbol == symbol]

    async def refresh(self, candidate: Candidate, now: datetime):
        await self._latency()
        if not self.connected:
            raise ConnectionError('fake broker disconnected')
        if not self.manual_clock:
            await self.discover(candidate.symbol, now)
        current = self.candidates.get(candidate.candidate_id)
        if current is None or current.conid != candidate.conid:
            raise ValueError('unknown contract')
        return current.model_copy(deep=True)

    async def submit(self, intent: OrderIntent):
        await self._latency()
        existing = next((o for o in self.orders.values() if o.ref == intent.ref), None)
        if existing:
            return existing.model_copy(deep=True)
        if self.pre_submit is None:
            raise ValueError('pre-submit safety callback required')
        current = self.candidates.get(intent.candidate.candidate_id)
        if current is None or current.model_dump() != intent.candidate.model_dump():
            raise ValueError('contract was not freshly verified')
        now = self.now if self.manual_clock else utcnow()
        submission_guard(intent, self._state(), self.config, now)
        self.pre_submit(intent)
        self._counter += 1
        order = BrokerOrder(order_id=f'F{self._counter}', ref=intent.ref, conid=current.conid,
                            symbol=current.symbol, intent=intent.intent, quantity=intent.quantity,
                            limit_price=intent.limit_price, status='Rejected' if self.reject_next else 'Submitted',
                            account_id=self.account.account_id)
        self.reject_next = False
        self.orders[order.order_id] = order
        if self.auto_fill:
            self._try_fill(order)
        self.events.put_nowait({'kind': 'state'})
        if self.fail_after_submit:
            self.fail_after_submit = False
            raise ConnectionError('simulated connection loss after accepting order')
        return order.model_copy(deep=True)

    def _try_fill(self, order: BrokerOrder):
        candidate = next((c for c in self.candidates.values() if c.conid == order.conid), None)
        if order.status in TERMINAL or candidate is None or candidate.expiry <= self.now.date():
            return
        if candidate.volume is None or candidate.volume <= 0 or candidate.bid is None or candidate.ask is None:
            return
        if not 0 <= (self.now - candidate.timestamp).total_seconds() <= self.config.max_data_age_seconds:
            return
        price = candidate.bid - self.config.slippage if order.intent == 'SELL_TO_CLOSE' else candidate.ask + self.config.slippage
        crossed = price >= order.limit_price if order.intent == 'SELL_TO_CLOSE' else price <= order.limit_price
        if price > 0 and crossed:
            self.partial_fill(order.order_id, order.quantity - order.filled, price)

    def partial_fill(self, order_id: str, quantity: float, price: float | None = None):
        order = self.orders[order_id]
        if order.status in TERMINAL or not math.isfinite(quantity) or not 0 < quantity <= order.quantity - order.filled:
            raise ValueError('invalid partial fill')
        candidate = next((c for c in self.candidates.values() if c.conid == order.conid), None)
        if candidate is None or candidate.expiry <= self.now.date() or not candidate.volume:
            raise ValueError('contract unavailable or expired')
        price = price if price is not None else order.limit_price
        if not math.isfinite(price) or price <= 0 or (price < order.limit_price if order.intent == 'SELL_TO_CLOSE' else price > order.limit_price):
            raise ValueError('fill violates limit')
        commission = self.config.commission_per_contract * quantity
        amount = price * candidate.multiplier * quantity
        position = self.positions.get(order.conid)
        if order.intent == 'SELL_TO_CLOSE':
            if position is None or position.quantity < quantity:
                raise ValueError('short options forbidden')
            self.account.cash += amount - commission
            self.account.realized_pnl += (price - position.average_cost) * quantity * candidate.multiplier - commission
            position.quantity -= quantity
        else:
            if self.account.cash < amount + commission:
                raise ValueError('insufficient cash at fill')
            self.account.cash -= amount + commission
            self.account.realized_pnl -= commission
            if position is None:
                position = Position(conid=order.conid, symbol=order.symbol, quantity=0, average_cost=price)
                self.positions[order.conid] = position
            position.average_cost = (position.average_cost * position.quantity + price * quantity) / (position.quantity + quantity)
            position.quantity += quantity
        order.filled += quantity
        order.status = 'Filled' if abs(order.quantity - order.filled) < 1e-9 else 'Submitted'
        key = f'{order.order_id}.{len(self.fills) + 1}'
        fill = Fill(execution_id=key, order_id=order.order_id, ref=order.ref, conid=order.conid,
                    quantity=quantity, price=price, commission=commission, timestamp=self.now)
        self.fills[key] = fill
        self._mark()
        self.events.put_nowait({'kind': 'state'})
        return fill.model_copy(deep=True)

    fill_order = partial_fill

    def _mark(self):
        unrealized, value = 0.0, 0.0
        for position in self.positions.values():
            candidate = next((c for c in self.candidates.values() if c.conid == position.conid), None)
            if candidate:
                mark = 0 if candidate.expiry <= self.now.date() else candidate.bid
                if mark is not None:
                    position.market_price = mark
            mark = position.market_price if position.market_price is not None else position.average_cost
            value += mark * position.quantity * 100
            unrealized += (mark - position.average_cost) * position.quantity * 100
        self.account.unrealized_pnl = unrealized
        self.account.net_liquidation = max(.000001, self.account.cash + value)
        self.account.daily_pnl = self.account.realized_pnl + unrealized

    async def cancel(self, order_id: str):
        await self._latency()
        if not self.connected:
            raise ConnectionError('fake broker disconnected')
        order = self.orders[order_id]
        if order.status not in TERMINAL:
            order.status = 'Cancelled'
        self.events.put_nowait({'kind': 'state'})

    async def next_event(self, timeout: float = 1):
        if not self.events.empty():
            return self.events.get_nowait()
        if self.manual_clock or not self.connected:
            try:
                return await asyncio.wait_for(self.events.get(), timeout)
            except asyncio.TimeoutError:
                return None
        loop = asyncio.get_running_loop()
        delay = max(0, 1 - (loop.time() - self._last_tick))
        if delay > timeout:
            await asyncio.sleep(timeout)
            return None
        await asyncio.sleep(delay)
        self._last_tick = loop.time()
        self.now = utcnow()
        self.account.timestamp = self.now
        self._tick += 1
        for offset, symbol in enumerate(self.config.symbols):
            price = 100 + offset * 50 + self._tick * .003 + math.sin(self._tick / 60) * .01
            self.events.put_nowait({'kind': 'tick', 'symbol': symbol, 'timestamp': self.now,
                                   'price': price, 'volume': 10.0})
        return self.events.get_nowait()
