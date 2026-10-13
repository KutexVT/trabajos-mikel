from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


Mode = Literal['OBSERVE', 'PAPER_APPROVAL', 'PAPER_AUTO', 'LIVE_APPROVAL', 'LIVE_AUTO']
Intent = Literal['BUY_TO_OPEN_CALL', 'BUY_TO_OPEN_PUT', 'SELL_TO_CLOSE']


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, validate_assignment=True)


class Candidate(Model):
    candidate_id: str = Field(min_length=1, max_length=120)
    conid: int = Field(gt=0)
    symbol: str
    right: Literal['CALL', 'PUT']
    expiry: date
    strike: float = Field(gt=0)
    multiplier: int = Field(gt=0)
    exchange: str = Field(min_length=1)
    currency: str = 'USD'
    bid: float | None
    ask: float | None
    mid: float | None
    last: float | None
    volume: float | None
    open_interest: float | None
    delta: float | None
    timestamp: AwareDatetime
    data_type: Literal['live', 'simulated', 'delayed', 'frozen', 'unknown'] = 'unknown'


class Bar(Model):
    symbol: str
    close_time: AwareDatetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    @model_validator(mode='after')
    def ordered(self):
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError('invalid OHLC')
        return self


class Account(Model):
    account_id: str = Field(min_length=1)
    environment: Literal['fake', 'paper', 'live', 'unknown']
    cash: float = Field(ge=0)
    net_liquidation: float = Field(gt=0)
    daily_pnl: float
    realized_pnl: float = 0
    unrealized_pnl: float = 0
    currency: str = 'USD'
    timestamp: AwareDatetime


class Position(Model):
    conid: int
    symbol: str
    quantity: float
    average_cost: float
    market_price: float | None = None


class OrderIntent(Model):
    ref: str
    account_id: str
    mode: Mode
    candidate: Candidate
    intent: Intent
    quantity: int = Field(ge=1, le=1, strict=True)
    limit_price: float = Field(gt=0)
    created_at: AwareDatetime
    order_type: Literal['LMT'] = 'LMT'


class BrokerOrder(Model):
    order_id: str
    ref: str
    conid: int
    symbol: str
    intent: Intent
    quantity: float
    filled: float = 0
    limit_price: float
    status: str
    account_id: str = ''


class Fill(Model):
    execution_id: str
    order_id: str
    ref: str
    conid: int
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    commission: float = Field(ge=0)
    timestamp: AwareDatetime


class BrokerState(Model):
    connected: bool
    account: Account
    positions: list[Position] = Field(default_factory=list)
    orders: list[BrokerOrder] = Field(default_factory=list)
    fills: list[Fill] = Field(default_factory=list)
    complete: bool = False
    critical_alerts: list[str] = Field(default_factory=list)


class Proposal(Model):
    action: Literal['HOLD', 'OPEN', 'CLOSE', 'REDUCE']
    candidate_id: str | None
    intent: Intent | None
    quantity: int = Field(ge=0, le=1, strict=True)
    order_type: Literal['LMT'] | None
    limit_price: float | None
    valid_until: AwareDatetime
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    reason: str = Field(max_length=500)
    @model_validator(mode='after')
    def consistent(self):
        if self.action == 'HOLD':
            if self.quantity != 0 or any(x is not None for x in (self.candidate_id, self.intent, self.order_type, self.limit_price)):
                raise ValueError('HOLD must not contain an order')
        else:
            if not self.candidate_id or self.quantity != 1 or self.order_type != 'LMT' or self.limit_price is None or self.limit_price <= 0:
                raise ValueError('incomplete order')
            if (self.action == 'OPEN') != (self.intent in ('BUY_TO_OPEN_CALL', 'BUY_TO_OPEN_PUT')):
                raise ValueError('action and intent mismatch')
            if self.action in ('CLOSE', 'REDUCE') and self.intent != 'SELL_TO_CLOSE':
                raise ValueError('short options forbidden')
        return self


def hold(now: datetime, reason: str) -> Proposal:
    return Proposal(action='HOLD', candidate_id=None, intent=None, quantity=0,
                    order_type=None, limit_price=None, valid_until=now,
                    confidence=0, evidence_ids=[], reason=reason[:500])


class Snapshot(Model):
    snapshot_id: str
    symbol: str
    timestamp: AwareDatetime
    bar: Bar
    candidates: list[Candidate]
    signal: str
    evidence_ids: list[str]
    positions: list[Position] = Field(default_factory=list)


class RiskResult(Model):
    allowed: bool
    reasons: list[str]
    estimated_cost: float = 0
