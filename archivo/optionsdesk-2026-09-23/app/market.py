"""UTC bars and deterministic screening. No empty candles or forward-filled prices."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.config import Config
from app.models import Bar, Candidate, Position, Snapshot


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone required')
    return value.astimezone(timezone.utc)


class CandleBuilder:
    """volume is incremental trade size, never a cumulative session volume."""

    def __init__(self):
        self._active: dict[str, dict] = {}
        self._last_tick: dict[str, datetime] = {}
        self._closed: dict[str, datetime] = {}
        self.rejected_ticks = 0
        self.last_error: str | None = None

    def feed(self, tick: dict) -> list[Bar]:
        try:
            symbol = tick['symbol']
            timestamp = _utc(tick['timestamp'])
            close_time = timestamp.replace(second=0, microsecond=0) + timedelta(minutes=1)
            if isinstance(tick['price'], bool) or isinstance(tick.get('volume'), bool):
                raise ValueError('invalid tick type')
            price = float(tick['price'])
            volume = float(tick.get('volume', 0))
            if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z][A-Z0-9.]{0,9}', symbol):
                raise ValueError('invalid symbol')
            if not math.isfinite(price) or price <= 0 or not math.isfinite(volume) or volume < 0:
                raise ValueError('invalid tick')
            if timestamp < self._last_tick.get(symbol, timestamp) or timestamp < self._closed.get(symbol, timestamp):
                raise ValueError('late tick')
            active = self._active.get(symbol)
            if active and timestamp < active['close_time'] and not math.isfinite(active['volume'] + volume):
                raise ValueError('volume overflow')
        except (KeyError, TypeError, ValueError, OverflowError):
            self.rejected_ticks += 1
            self.last_error = 'invalid_or_out_of_order_tick'
            return []

        closed = []
        active = self._active.get(symbol)
        if active and timestamp >= active['close_time']:
            closed.append(Bar.model_validate(active))
            self._closed[symbol] = active['close_time']
            active = None
        if active is None:
            active = dict(symbol=symbol,
                          close_time=close_time,
                          open=price, high=price, low=price, close=price, volume=volume)
            self._active[symbol] = active
        else:
            active['high'] = max(active['high'], price)
            active['low'] = min(active['low'], price)
            active['close'] = price
            active['volume'] += volume
        self._last_tick[symbol] = timestamp
        return closed

    def flush(self, now: datetime) -> list[Bar]:
        now = _utc(now)
        closed = []
        for symbol, active in list(self._active.items()):
            if active['close_time'] <= now:
                closed.append(Bar.model_validate(active))
                self._closed[symbol] = active['close_time']
                del self._active[symbol]
        return sorted(closed, key=lambda bar: (bar.close_time, bar.symbol))


def select_candidates(candidates: Iterable[Candidate], config: Config, now: datetime) -> list[Candidate]:
    """Bounded candidates with actual two-sided liquid quotes and no future data."""
    now = _utc(now)
    valid = []
    for raw in candidates:
        try:
            c = Candidate.model_validate(raw.model_dump() if isinstance(raw, Candidate) else raw)
            age = (now - c.timestamp).total_seconds()
            dte = (c.expiry - now.date()).days
            if c.symbol not in config.symbols or c.currency != 'USD':
                continue
            if not config.min_dte <= dte <= config.max_dte or not 0 <= age <= config.max_data_age_seconds:
                continue
            if c.data_type not in ('live', 'simulated') or c.bid is None or c.ask is None or c.mid is None:
                continue
            if c.bid <= 0 or c.ask < c.bid or not math.isclose(c.mid, (c.bid + c.ask) / 2, rel_tol=1e-6, abs_tol=1e-8):
                continue
            if (c.ask - c.bid) / c.mid > config.max_spread_pct:
                continue
            if (c.volume or 0) < 0 or (c.open_interest or 0) < 0 or max(c.volume or 0, c.open_interest or 0) <= 0:
                continue
            if c.last is not None and c.last <= 0 or c.delta is not None and not -1 <= c.delta <= 1:
                continue
            valid.append(c)
        except (ValueError, TypeError):
            continue
    ids = Counter(c.candidate_id for c in valid)
    conids = Counter(c.conid for c in valid)
    valid = [c for c in valid if ids[c.candidate_id] == 1 and conids[c.conid] == 1]
    valid.sort(key=lambda c: ((c.ask - c.bid) / c.mid, -(c.volume or 0), c.expiry, c.candidate_id))
    return valid[:config.max_candidates]


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()[:24]


def make_snapshot(symbol: str, bar: Bar, bars: Iterable[Bar], candidates: Iterable[Candidate],
                  positions: Iterable[Position], config: Config) -> Snapshot:
    if bar.symbol != symbol or symbol not in config.symbols:
        raise ValueError('snapshot symbol mismatch')
    bar = Bar.model_validate(bar.model_dump())
    history = {b.close_time: b for b in bars if b.symbol == symbol and b.close_time <= bar.close_time}
    history[bar.close_time] = bar
    recent = sorted(history.values(), key=lambda b: b.close_time)[-6:]
    contiguous = len(recent) == 6 and all(b.close_time - a.close_time == timedelta(minutes=1)
                                        for a, b in zip(recent, recent[1:]))
    momentum = recent[-1].close / recent[0].close - 1 if contiguous else None
    signal = ('UP' if momentum >= config.signal_threshold else 'DOWN' if momentum <= -config.signal_threshold else 'NONE') if momentum is not None else 'NONE'
    selected = sorted((c for c in candidates if c.symbol == symbol), key=lambda c: c.candidate_id)[:config.max_candidates]
    owned = sorted((p for p in positions if p.symbol == symbol and p.quantity != 0), key=lambda p: p.conid)
    evidence = ['bar:' + _digest(bar.model_dump(mode='json'))]
    if momentum is not None:
        evidence.append(f'momentum5:{momentum:.8f}:' + _digest([b.model_dump(mode='json') for b in recent]))
    evidence.extend('candidate:' + c.candidate_id + ':' + _digest(c.model_dump(mode='json')) for c in selected)
    value = dict(symbol=symbol, timestamp=bar.close_time, bar=bar, candidates=selected,
                 signal=signal, evidence_ids=evidence, positions=owned)
    snapshot = Snapshot(snapshot_id='pending', **value)
    snapshot.snapshot_id = 'snapshot:' + _digest(snapshot.model_dump(mode='json', exclude={'snapshot_id'}))
    return snapshot
