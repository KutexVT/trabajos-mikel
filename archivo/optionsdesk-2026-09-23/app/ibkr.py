"""Official TWS socket API; no credentials, web automation or third-party broker SDK."""
from __future__ import annotations

import asyncio
import math
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from app.broker import TERMINAL, submission_guard
from app.config import Config
from app.models import Account, Bar, BrokerOrder, BrokerState, Candidate, Fill, OrderIntent, Position, utcnow

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.order_cancel import OrderCancel
    from ibapi.execution import ExecutionFilter
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and abs(number) < 1e100 else None
    except (ValueError, TypeError):
        return None


if SDK_AVAILABLE:
    class _Wire(EWrapper, EClient):
        def __init__(self, owner):
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.owner = owner

        def _emit(self, name, *args):
            loop = self.owner._loop
            if loop and not loop.is_closed():
                loop.call_soon_threadsafe(self.owner._dispatch, name, args)

        def nextValidId(self, orderId): self._emit('next_id', orderId)
        def managedAccounts(self, accountsList): self._emit('accounts', accountsList)
        def connectionClosed(self): self._emit('closed')
        def error(self, reqId, *args):
            # 10.50 adds errorTime; accept old callback signature for diagnostic mocks.
            code, message = args[1:3] if len(args) >= 3 and isinstance(args[1], int) else args[:2]
            self._emit('error', reqId, code, str(message))
        def contractDetails(self, reqId, contractDetails): self._emit('append', reqId, contractDetails.contract)
        def contractDetailsEnd(self, reqId): self._emit('done', reqId)
        def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId, tradingClass, multiplier, expirations, strikes):
            self._emit('append', reqId, (exchange, underlyingConId, tradingClass, multiplier, expirations, strikes))
        def securityDefinitionOptionParameterEnd(self, reqId): self._emit('done', reqId)
        def accountSummary(self, reqId, account, tag, value, currency): self._emit('account_value', reqId, account, tag, value, currency)
        def accountSummaryEnd(self, reqId): self._emit('done', reqId)
        def position(self, account, contract, pos, avgCost): self._emit('position', account, contract, pos, avgCost)
        def positionEnd(self): self._emit('done', 'positions')
        def openOrder(self, orderId, contract, order, orderState): self._emit('order', orderId, contract, order, orderState)
        def openOrderEnd(self): self._emit('done', 'orders')
        def completedOrder(self, contract, order, orderState): self._emit('order', order.orderId, contract, order, orderState)
        def completedOrdersEnd(self): self._emit('done', 'completed')
        def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
            self._emit('status', orderId, status, filled, permId, clientId)
        def execDetails(self, reqId, contract, execution): self._emit('execution', reqId, contract, execution)
        def execDetailsEnd(self, reqId): self._emit('done', reqId)
        def commissionAndFeesReport(self, report): self._emit('commission', report.execId, report.commissionAndFees, report.currency)
        def commissionReport(self, report): self._emit('commission', report.execId, report.commission, report.currency)
        def pnl(self, reqId, dailyPnL, unrealizedPnL, realizedPnL): self._emit('pnl', reqId, dailyPnL, unrealizedPnL, realizedPnL)
        def tickPrice(self, reqId, tickType, price, attrib): self._emit('price', reqId, tickType, price)
        def tickSize(self, reqId, tickType, size): self._emit('size', reqId, tickType, size)
        def marketDataType(self, reqId, marketDataType): self._emit('data_type', reqId, marketDataType)
        def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice):
            self._emit('delta', reqId, delta)
        def realtimeBar(self, reqId, time, open_, high, low, close, volume, wap, count):
            self._emit('bar', reqId, time, open_, high, low, close, volume)


class IBKRBroker:
    def __init__(self, config: Config):
        self.config = config
        self.pre_submit: Callable[[OrderIntent], None] | None = None
        self.connected = False
        self._loop = None
        self._wire = None
        self._thread = None
        self._future: dict = {}
        self._results: dict = {}
        self._request_id = 1000000
        self._next_order_id = None
        self._managed: list[str] = []
        self._account_id = os.environ.get('IBKR_ACCOUNT', '').strip()
        self._account_values: dict = {}
        self._pnl = None
        self._pnl_id = None
        self._positions: dict[int, Position] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._fills: dict[str, Fill] = {}
        self._executions: dict = {}
        self._commissions: dict = {}
        self._permanent_ids: dict[int, str] = {}
        self._quotes: dict = {}
        self._qualified: dict[int, object] = {}
        self._verified: dict[int, Candidate] = {}
        self._streams: dict[int, str] = {}
        self._bars: dict = {}
        self._alerts: set[str] = set()
        self._complete = False
        self._state_lock = asyncio.Lock()
        self._submit_lock = asyncio.Lock()
        self.events: asyncio.Queue = asyncio.Queue(maxsize=10000)

    def _event(self):
        if not self.events.full():
            self.events.put_nowait({'kind': 'state'})
        else:
            self._alerts.add('event queue overflow')
            self._complete = False

    def _dispatch(self, name, args):
        try:
            getattr(self, f'_on_{name}')(*args)
        except Exception as exc:
            self._alerts.add(f'invalid broker callback: {name}: {type(exc).__name__}')
            self._complete = False
            self._event()

    def _new_request(self, key=None):
        if key is None:
            self._request_id += 1
            key = self._request_id
        if key in self._future and not self._future[key].done():
            raise RuntimeError('overlapping broker request')
        self._future[key] = asyncio.get_running_loop().create_future()
        self._results[key] = []
        return key

    async def _wait(self, key):
        try:
            await asyncio.wait_for(self._future[key], self.config.ibkr_timeout_seconds)
            return self._results.get(key, [])
        finally:
            self._future.pop(key, None)
            self._results.pop(key, None)

    def _on_done(self, key):
        future = self._future.get(key)
        if future is not None and not future.done():
            future.set_result(True)

    def _on_append(self, key, value):
        if key in self._results:
            self._results[key].append(value)

    def _on_next_id(self, value):
        self._next_order_id = max(value, self._next_order_id or 0)
        self._on_done('ready')

    def _on_accounts(self, value):
        self._managed = [item for item in value.split(',') if item]
        self._on_done('accounts')

    def _on_closed(self):
        self.connected = False
        self._complete = False
        self._alerts.add('IBKR disconnected')
        for future in self._future.values():
            if not future.done(): future.set_exception(ConnectionError('IBKR disconnected'))
        self._event()

    def _on_error(self, req_id, code, message):
        if code in {2104, 2106, 2107, 2108, 2158}:
            return
        # Never record IB error strings: they can contain account/order details.
        if code in {1100, 1101, 1102, 1300, 2110, 504}:
            self._on_closed()
            return
        future = self._future.get(req_id)
        if future is not None and not future.done():
            future.set_exception(RuntimeError(f'IBKR request failed ({code})'))
        if req_id in self._quotes:
            self._quotes[req_id]['error'] = code
        order = self._orders.get(str(req_id))
        if order and code in {201, 202}:
            order.status = 'Rejected' if code == 201 else 'Cancelled'
        if req_id in self._streams or req_id == self._pnl_id or req_id < 0 or code in {10147, 10148}:
            self._alerts.add(f'IBKR error {code}')
            self._complete = False
        self._event()

    async def connect(self):
        if not SDK_AVAILABLE:
            raise RuntimeError('official IBKR SDK missing; install the official ZIP Python client')
        if not self._account_id:
            raise ValueError('IBKR_ACCOUNT required; no account is chosen automatically')
        if os.environ.get('IBKR_ENV') not in {'paper', 'live'}:
            raise ValueError('IBKR_ENV must be paper or live')
        if self.connected:
            return
        await self.disconnect()
        self._loop = asyncio.get_running_loop()
        self._alerts.clear()
        self._managed.clear()
        self._pnl = None
        self._account_values.clear()
        self._qualified.clear()
        self._verified.clear()
        self._wire = _Wire(self)
        ready, accounts = self._new_request('ready'), self._new_request('accounts')
        try:
            await asyncio.wait_for(asyncio.to_thread(self._wire.connect, self.config.ibkr_host,
                                   self.config.ibkr_port, self.config.ibkr_client_id), self.config.ibkr_timeout_seconds)
            self._thread = threading.Thread(target=self._wire.run, name='ibkr-reader', daemon=True)
            self._thread.start()
            await asyncio.gather(self._wait(ready), self._wait(accounts))
            if self._account_id not in self._managed or self._environment() != os.environ.get('IBKR_ENV'):
                raise ValueError('configured account absent or environment mismatch')
            self.connected = True
            self._wire.reqMarketDataType(1)
            self._pnl_id = self._new_request()
            self._wire.reqPnL(self._pnl_id, self._account_id, '')
            await self._wait(self._pnl_id)
            await self.state()
            for symbol in self.config.symbols:
                contract = await self._stock(symbol)
                self._request_id += 1
                self._streams[self._request_id] = symbol
                self._wire.reqRealTimeBars(self._request_id, contract, 5, 'TRADES', True, [])
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self):
        self.connected = False
        self._complete = False
        if self._wire is not None:
            self._wire.disconnect()
        if self._thread and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 2)
        for future in self._future.values():
            if not future.done(): future.cancel()
        self._future.clear()
        self._results.clear()
        self._quotes.clear()
        self._streams.clear()
        self._bars.clear()
        self._event()

    def _environment(self):
        if self._account_id not in self._managed: return 'unknown'
        if re.fullmatch(r'DU\d+', self._account_id): return 'paper'
        if re.fullmatch(r'U\d+', self._account_id): return 'live'
        return 'unknown'

    def _on_account_value(self, req_id, account, tag, value, currency):
        if account == self._account_id and currency == 'USD':
            self._account_values[tag] = (_number(value), utcnow())

    def _on_pnl(self, req_id, daily, unrealized, realized):
        if req_id != self._pnl_id: return
        numbers = [_number(v) for v in (daily, unrealized, realized)]
        if None not in numbers:
            self._pnl = (*numbers, utcnow())
            self._on_done(req_id)
        self._event()

    def _account(self):
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        cash, cash_time = self._account_values.get('TotalCashValue', (None, epoch))
        nav, nav_time = self._account_values.get('NetLiquidation', (None, epoch))
        daily, unrealized, realized, pnl_time = self._pnl or (0, 0, 0, epoch)
        valid = cash is not None and cash >= 0 and nav is not None and nav > 0 and self._pnl is not None
        if not valid: self._complete = False
        return Account(account_id=self._account_id or 'UNCONFIGURED', environment=self._environment(),
                       cash=cash if cash is not None and cash >= 0 else 0,
                       net_liquidation=nav if nav is not None and nav > 0 else .000001,
                       daily_pnl=daily, realized_pnl=realized, unrealized_pnl=unrealized,
                       timestamp=min(cash_time, nav_time, pnl_time))

    async def state(self):
        async with self._state_lock:
            if self.connected:
                self._complete = False
                self._positions.clear()
                summary = self._new_request()
                execution = self._new_request()
                keys = [self._new_request(k) for k in ('positions', 'orders', 'completed')]
                self._account_values.clear()
                try:
                    self._wire.reqAccountSummary(summary, 'All', 'TotalCashValue,NetLiquidation')
                    self._wire.cancelPositions()
                    self._wire.reqPositions()
                    self._wire.reqAllOpenOrders()
                    self._wire.reqCompletedOrders(False)
                    filter_ = ExecutionFilter()
                    filter_.acctCode = self._account_id
                    self._wire.reqExecutions(execution, filter_)
                    results = await asyncio.gather(*(self._wait(k) for k in [summary, execution, *keys]), return_exceptions=True)
                    if any(isinstance(result, BaseException) for result in results):
                        self._alerts.add('incomplete account/order/execution reconciliation')
                    else:
                        self._alerts.discard('incomplete account/order/execution reconciliation')
                        self._complete = True
                    # Commission callbacks can follow execDetailsEnd. Never treat unknown fees as zero.
                    if set(self._executions) - set(self._commissions):
                        self._complete = False
                finally:
                    self._wire.cancelAccountSummary(summary)
            account = self._account()
            if (utcnow() - account.timestamp).total_seconds() > self.config.max_data_age_seconds:
                self._complete = False
            return BrokerState(connected=self.connected, account=account,
                               positions=[p.model_copy(deep=True) for p in self._positions.values() if p.quantity],
                               orders=[o.model_copy(deep=True) for o in self._orders.values()],
                               fills=[f.model_copy(deep=True) for f in self._fills.values()],
                               complete=self._complete, critical_alerts=sorted(self._alerts))

    def _on_position(self, account, contract, quantity, average_cost):
        if account != self._account_id: return
        multiplier = _number(contract.multiplier) or 1
        self._positions[contract.conId] = Position(conid=contract.conId, symbol=contract.symbol,
                                                  quantity=float(quantity), average_cost=average_cost / multiplier)
        if contract.secType == 'OPT' and float(quantity) < 0:
            self._alerts.add('short option position present')
        self._event()

    def _order_key(self, order_id, client_id, perm_id=0):
        if perm_id and perm_id in self._permanent_ids: return self._permanent_ids[perm_id]
        key = str(order_id) if client_id == self.config.ibkr_client_id and order_id >= 0 else f'external:{client_id}:{order_id}:{perm_id}'
        if perm_id: self._permanent_ids[perm_id] = key
        return key

    def _on_order(self, order_id, contract, order, state):
        if order.account != self._account_id: return
        key = self._order_key(order_id, order.clientId, order.permId)
        status = state.status or getattr(state, 'completedStatus', '') or 'Unknown'
        previous = self._orders.get(key)
        intent = 'SELL_TO_CLOSE' if order.action == 'SELL' else ('BUY_TO_OPEN_PUT' if contract.right == 'P' else 'BUY_TO_OPEN_CALL')
        filled = previous.filled if previous else 0
        if status == 'Filled': filled = float(order.totalQuantity)
        self._orders[key] = BrokerOrder(order_id=key, ref=order.orderRef or f'unmanaged:{order.permId}',
                                       conid=contract.conId, symbol=contract.symbol, intent=intent,
                                       quantity=float(order.totalQuantity), filled=filled,
                                       limit_price=_number(order.lmtPrice) or 0, status=status, account_id=order.account)
        if status not in TERMINAL and (contract.secType != 'OPT' or order.orderType != 'LMT' or order.clientId != self.config.ibkr_client_id):
            self._alerts.add('unmanaged active order requires review')
        if order_id >= 0: self._next_order_id = max(self._next_order_id or 0, order_id + 1)
        self._event()

    def _on_status(self, order_id, status, filled, perm_id, client_id):
        key = self._order_key(order_id, client_id, perm_id)
        if key in self._orders:
            self._orders[key].status = status
            self._orders[key].filled = float(filled)
        else:
            self._complete = False
        self._event()

    def _on_execution(self, req_id, contract, execution):
        if execution.acctNumber != self._account_id: return
        self._executions[execution.execId] = (contract, execution)
        self._publish_fill(execution.execId)
        self._event()

    def _on_commission(self, execution_id, commission, currency):
        value = _number(commission)
        if value is None or value < 0 or currency != 'USD':
            self._alerts.add('missing or unsupported execution commission')
            return
        self._commissions[execution_id] = value
        self._publish_fill(execution_id)
        self._event()

    def _publish_fill(self, execution_id):
        if execution_id not in self._executions or execution_id not in self._commissions: return
        contract, execution = self._executions[execution_id]
        text = ' '.join(execution.time.split())
        match = re.fullmatch(r'(\d{8})[ -](\d{2}:\d{2}:\d{2})(?: (.+))?', text)
        if not match: raise ValueError('unrecognized execution timestamp')
        zone = match[3] or os.environ.get('IBKR_TIMEZONE', 'UTC')
        stamp = datetime.strptime(f'{match[1]} {match[2]}', '%Y%m%d %H:%M:%S').replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
        key = self._order_key(execution.orderId, execution.clientId, execution.permId)
        self._fills[execution_id] = Fill(execution_id=execution_id, order_id=key, ref=execution.orderRef or f'unmanaged:{execution.permId}',
                                         conid=contract.conId, quantity=float(execution.shares), price=execution.price,
                                         commission=self._commissions[execution_id], timestamp=stamp)

    async def _details(self, contract):
        request = self._new_request()
        self._wire.reqContractDetails(request, contract)
        return await self._wait(request)

    async def _stock(self, symbol):
        contract = Contract()
        contract.symbol, contract.secType, contract.exchange, contract.currency = symbol, 'STK', 'SMART', 'USD'
        details = await self._details(contract)
        if len(details) != 1 or details[0].conId <= 0: raise ValueError('ambiguous stock contract')
        return details[0]

    def _quote_done(self, request):
        quote = self._quotes.get(request, {})
        if quote.get('data_type') is not None and quote.get('bid') is not None and quote.get('ask') is not None:
            self._on_done(request)

    def _on_price(self, request, tick_type, value):
        quote = self._quotes.get(request)
        if quote is None: return
        field = {1: 'bid', 2: 'ask', 4: 'last', 66: 'bid', 67: 'ask', 68: 'last'}.get(tick_type)
        if field:
            quote[field] = _number(value)
            quote[f'{field}_time'] = utcnow()
        if tick_type in {66, 67, 68}: quote['data_type'] = 3
        self._quote_done(request)

    def _on_size(self, request, tick_type, value):
        quote = self._quotes.get(request)
        if quote is None: return
        field = {8: 'volume', 74: 'volume', 27: 'call_oi', 28: 'put_oi', 29: 'call_volume', 30: 'put_volume'}.get(tick_type)
        if field: quote[field] = _number(value)

    def _on_data_type(self, request, value):
        if request in self._quotes: self._quotes[request]['data_type'] = value
        self._quote_done(request)

    def _on_delta(self, request, value):
        if request in self._quotes and _number(value) is not None and -1 <= value <= 1:
            self._quotes[request]['delta'] = value

    async def _quote(self, contract):
        request = self._new_request()
        self._quotes[request] = {}
        self._wire.reqMktData(request, contract, '100,101' if contract.secType == 'OPT' else '', False, False, [])
        try:
            await self._wait(request)
            return dict(self._quotes[request])
        finally:
            self._wire.cancelMktData(request)
            self._quotes.pop(request, None)

    async def _candidate(self, contract, candidate_id=None):
        quote = await self._quote(contract)
        right = {'C': 'CALL', 'P': 'PUT'}.get(contract.right)
        if right is None or contract.secType != 'OPT' or contract.conId <= 0: raise ValueError('invalid option identity')
        bid, ask = quote.get('bid'), quote.get('ask')
        candidate = Candidate(candidate_id=candidate_id or f'IB-{contract.conId}', conid=contract.conId,
                              symbol=contract.symbol, right=right, expiry=datetime.strptime(contract.lastTradeDateOrContractMonth[:8], '%Y%m%d').date(),
                              strike=contract.strike, multiplier=int(contract.multiplier), exchange=contract.exchange or 'SMART',
                              currency=contract.currency, bid=bid, ask=ask, mid=(bid + ask) / 2 if bid is not None and ask is not None else None,
                              last=quote.get('last'), volume=quote.get('call_volume' if right == 'CALL' else 'put_volume', quote.get('volume')),
                              open_interest=quote.get('call_oi' if right == 'CALL' else 'put_oi'), delta=quote.get('delta'),
                              timestamp=min(quote['bid_time'], quote['ask_time']),
                              data_type={1: 'live', 2: 'frozen', 3: 'delayed', 4: 'delayed'}.get(quote.get('data_type'), 'unknown'))
        self._qualified[contract.conId] = contract
        self._verified[contract.conId] = candidate.model_copy(deep=True)
        return candidate

    async def discover(self, symbol: str, now: datetime):
        if not self.connected or symbol not in self.config.symbols: return []
        stock = await self._stock(symbol)
        quote = await self._quote(stock)
        if quote.get('data_type') != 1 or not quote.get('bid') or not quote.get('ask'): return []
        spot = (quote['bid'] + quote['ask']) / 2
        request = self._new_request()
        self._wire.reqSecDefOptParams(request, symbol, '', 'STK', stock.conId)
        chains = await self._wait(request)
        chains = sorted((chain for chain in chains if chain[0] == 'SMART' and chain[1] == stock.conId and chain[3] == '100'), key=lambda c: c[2])
        result = []
        for exchange, _, trading_class, multiplier, expiries, strikes in chains:
            eligible = sorted(expiry for expiry in expiries if self.config.min_dte <= (datetime.strptime(expiry, '%Y%m%d').date() - now.date()).days <= self.config.max_dte)
            for expiry in eligible[:2]:
                for strike in sorted(strikes, key=lambda value: (abs(value - spot), value))[:2]:
                    for right in ('C', 'P'):
                        contract = Contract()
                        contract.symbol, contract.secType, contract.exchange, contract.currency = symbol, 'OPT', exchange, 'USD'
                        contract.lastTradeDateOrContractMonth, contract.strike, contract.right = expiry, strike, right
                        contract.multiplier, contract.tradingClass = multiplier, trading_class
                        try:
                            details = await self._details(contract)
                            if len(details) != 1 or details[0].conId <= 0: continue
                            candidate = await self._candidate(details[0])
                            if not any(c.conid == candidate.conid for c in result): result.append(candidate)
                        except (TimeoutError, ValueError, RuntimeError):
                            continue
                        if len(result) >= self.config.max_candidates: return result
        return result

    async def refresh(self, candidate: Candidate, now: datetime):
        if not self.connected or candidate.conid not in self._qualified:
            raise ValueError('disconnected or unknown contract')
        query = Contract()
        query.conId, query.exchange = candidate.conid, candidate.exchange
        details = await self._details(query)
        if len(details) != 1: raise ValueError('contract identity no longer unique')
        current = await self._candidate(details[0], candidate.candidate_id)
        fields = ('conid', 'symbol', 'right', 'expiry', 'strike', 'multiplier', 'currency')
        if any(getattr(candidate, field) != getattr(current, field) for field in fields):
            raise ValueError('contract identity changed')
        return current

    async def submit(self, intent: OrderIntent):
        async with self._submit_lock:
            if self.pre_submit is None: raise ValueError('pre-submit safety callback required')
            state = await self.state()
            existing = next((order for order in state.orders if order.ref == intent.ref), None)
            if existing: return existing
            if intent.mode.startswith('LIVE_') and (os.environ.get('IBKR_ENV') != 'live' or os.environ.get('LIVE_TRADING_ENABLED') != 'true'):
                raise ValueError('live trading disabled')
            if os.environ.get('IBKR_ENV') != self._environment(): raise ValueError('environment mismatch')
            current = await self.refresh(intent.candidate, utcnow())
            old_mid, new_mid = intent.candidate.mid, current.mid
            if old_mid is None or new_mid is None or old_mid <= 0 or abs(new_mid - old_mid) / old_mid > self.config.max_quote_change_pct:
                raise ValueError('quote changed materially')
            checked = intent.model_copy(update={'candidate': current})
            submission_guard(checked, state, self.config, utcnow())
            if self._next_order_id is None: raise ValueError('no broker order id')
            order = Order()
            order.account, order.orderRef = self._account_id, intent.ref
            order.action = 'SELL' if intent.intent == 'SELL_TO_CLOSE' else 'BUY'
            order.orderType, order.totalQuantity, order.lmtPrice = 'LMT', Decimal(intent.quantity), intent.limit_price
            order.tif, order.outsideRth, order.transmit = 'DAY', False, True
            order.openClose = 'C' if intent.intent == 'SELL_TO_CLOSE' else 'O'
            order_id = self._next_order_id
            self._next_order_id += 1
            result = BrokerOrder(order_id=str(order_id), ref=intent.ref, conid=current.conid, symbol=current.symbol,
                                 intent=intent.intent, quantity=intent.quantity, limit_price=intent.limit_price,
                                 status='PendingSubmit', account_id=self._account_id)
            # No await after the final persisted gate; callback rechecks kill/mode/session approval.
            self.pre_submit(checked)
            self._orders[str(order_id)] = result
            self._wire.placeOrder(order_id, self._qualified[current.conid], order)
            self._complete = False
            self._event()
            return result.model_copy(deep=True)

    async def cancel(self, order_id: str):
        if not self.connected: raise ConnectionError('IBKR disconnected')
        order = self._orders.get(order_id)
        if not order or not order_id.isdecimal() or order.account_id != self._account_id:
            raise ValueError('only this client account orders can be cancelled')
        if order.status not in TERMINAL:
            self._wire.cancelOrder(int(order_id), OrderCancel())
            order.status = 'PendingCancel'
            self._event()

    def _on_bar(self, request, epoch, open_, high, low, close, volume):
        if request not in self._streams: return
        stamp = datetime.fromtimestamp(epoch, timezone.utc)
        minute = stamp.replace(second=0, microsecond=0)
        symbol = self._streams[request]
        previous = self._bars.get(symbol)
        if previous is None or previous['minute'] != minute:
            previous = {'minute': minute, 'count': 0, 'last': None, 'open': open_, 'high': high, 'low': low, 'close': close, 'volume': 0.0}
            self._bars[symbol] = previous
        if previous['last'] is not None and epoch <= previous['last']: return
        if previous['last'] is not None and epoch - previous['last'] != 5: previous['count'] = -100
        previous.update(last=epoch, count=previous['count'] + 1, high=max(previous['high'], high), low=min(previous['low'], low), close=close, volume=previous['volume'] + float(volume))
        if stamp.second == 55 and previous['count'] == 12:
            bar = Bar(symbol=symbol, close_time=minute + timedelta(minutes=1), **{key: previous[key] for key in ('open', 'high', 'low', 'close', 'volume')})
            if self.events.full():
                self._alerts.add('event queue overflow')
                self._complete = False
            else:
                self.events.put_nowait({'kind': 'bar', 'bar': bar})

    async def next_event(self, timeout: float = 1):
        try:
            return await asyncio.wait_for(self.events.get(), timeout)
        except asyncio.TimeoutError:
            return None
