import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.analyst import OpenAIAnalyst, PROPOSAL_SCHEMA, ScriptedAnalyst
from app.config import Config
from app.market import CandleBuilder, make_snapshot, select_candidates
from app.models import Bar, Candidate, Position, hold


NOW = datetime(2026, 9, 23, 15, 5, tzinfo=timezone.utc)


def candidate(**changes):
    values = dict(candidate_id='AAPL-call', conid=1234, symbol='AAPL', right='CALL',
                  expiry=(NOW + timedelta(days=30)).date(), strike=100, multiplier=100,
                  exchange='SMART', currency='USD', bid=.30, ask=.32, mid=.31,
                  last=.31, volume=100, open_interest=1000, delta=.4,
                  timestamp=NOW, data_type='simulated')
    values.update(changes)
    return Candidate.model_validate(values)


def snapshot(config=None):
    bars = [Bar(symbol='AAPL', close_time=NOW - timedelta(minutes=5-i),
                open=100+i, high=100+i, low=100+i, close=100+i, volume=100)
            for i in range(6)]
    return make_snapshot('AAPL', bars[-1], bars, [candidate()], [], config or Config())


def response(text, **changes):
    values = dict(status='completed', output_text=text, output=[],
                  usage=SimpleNamespace(input_tokens=150, output_tokens=20, total_tokens=170))
    values.update(changes)
    return SimpleNamespace(**values)


class MarketTests(unittest.TestCase):
    def test_candles_close_on_boundary_in_utc_without_empty_bars(self):
        builder = CandleBuilder()
        self.assertEqual(builder.feed(dict(symbol='AAPL', timestamp='2026-09-23T09:00:01-06:00', price=100, volume=2)), [])
        self.assertEqual(builder.feed(dict(symbol='AAPL', timestamp='2026-09-23T15:00:30Z', price=102, volume=3)), [])
        self.assertEqual(builder.flush(NOW.replace(minute=0, second=59)), [])
        closed = builder.feed(dict(symbol='AAPL', timestamp='2026-09-23T15:03:00Z', price=99, volume=1))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].close_time, NOW.replace(minute=1))
        self.assertEqual((closed[0].open, closed[0].high, closed[0].low, closed[0].close, closed[0].volume), (100, 102, 100, 102, 5))
        self.assertEqual(builder.feed(dict(symbol='AAPL', timestamp='2026-09-23T15:02:59Z', price=900, volume=9)), [])
        self.assertEqual(builder.rejected_ticks, 1)
        self.assertEqual([b.close_time for b in builder.flush(NOW)], [NOW.replace(minute=4)])
        self.assertEqual(builder.flush(NOW), [])
        # Late arrival must not resurrect a previously emitted bar.
        self.assertEqual(builder.feed(dict(symbol='AAPL', timestamp='2026-09-23T15:03:30Z', price=900)), [])
        self.assertEqual(builder.flush(NOW), [])

    def test_tick_quality_and_sorted_multi_symbol_flush(self):
        builder = CandleBuilder()
        for tick in ({}, dict(symbol='AAPL', timestamp=NOW.replace(tzinfo=None), price=100),
                     dict(symbol='AAPL', timestamp=NOW, price=float('nan')),
                     dict(symbol='AAPL', timestamp=NOW, price=100, volume=-1)):
            self.assertEqual(builder.feed(tick), [])
        self.assertEqual(builder.rejected_ticks, 4)
        for symbol, stamp in [('MSFT', NOW), ('AAPL', NOW - timedelta(minutes=1))]:
            builder.feed(dict(symbol=symbol, timestamp=stamp, price=100))
        closed = builder.flush(NOW + timedelta(minutes=1))
        self.assertEqual([b.symbol for b in closed], ['AAPL', 'MSFT'])
        builder.feed(dict(symbol='AAPL', timestamp=NOW + timedelta(minutes=2), price=100, volume=1e308))
        self.assertEqual(builder.feed(dict(symbol='AAPL', timestamp=NOW + timedelta(minutes=2, seconds=1), price=100, volume=1e308)), [])
        self.assertEqual(builder.flush(NOW + timedelta(minutes=3))[0].volume, 1e308)

    def test_candidate_screen_rejects_bad_quality_and_bounds_selection(self):
        config = Config(max_candidates=1)
        bad_changes = [dict(bid=0), dict(bid=None), dict(ask=None), dict(mid=None),
                       dict(ask=.50, mid=.4), dict(mid=.29), dict(expiry=NOW.date()),
                       dict(timestamp=NOW - timedelta(seconds=31)), dict(timestamp=NOW + timedelta(seconds=1)),
                       dict(data_type='delayed'), dict(data_type='frozen'), dict(symbol='NVDA'),
                       dict(volume=0, open_interest=0), dict(volume=-1), dict(currency='EUR')]
        for changes in bad_changes:
            with self.subTest(changes=changes):
                self.assertEqual(select_candidates([candidate(**changes)], config, NOW), [])
        self.assertEqual(select_candidates([candidate(), candidate()], config, NOW), [])
        choices = [candidate(candidate_id='less-liquid', conid=5678, volume=10), candidate()]
        self.assertEqual([c.candidate_id for c in select_candidates(choices, config, NOW)], ['AAPL-call'])
        self.assertEqual(select_candidates([{'conid': 'bad'}], config, NOW), [])

    def test_snapshot_stable_momentum_ignores_future_and_missing_history(self):
        first = snapshot()
        self.assertEqual(first.signal, 'UP')
        self.assertEqual(first.model_dump(), snapshot().model_dump())
        future = first.bar.model_copy(update={'close_time': NOW + timedelta(minutes=1), 'close': 10000})
        fresh = make_snapshot('AAPL', first.bar, [future], first.candidates, [], Config())
        self.assertEqual(fresh.signal, 'NONE')
        self.assertFalse(any(e.startswith('momentum5:') for e in fresh.evidence_ids))
        flat = [first.bar.model_copy(update={'close_time': NOW - timedelta(minutes=i)}) for i in range(6)]
        self.assertEqual(make_snapshot('AAPL', first.bar, flat, first.candidates, [], Config()).signal, 'NONE')


class AnalystTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = Config()
        self.snapshot = snapshot(self.config)
        self.analyst = OpenAIAnalyst(self.config)
        self.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock()), close=AsyncMock())
        self.analyst._client = self.client

    async def test_strict_schema_and_real_responses_request_contract(self):
        def check(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
                    self.assertEqual(set(node['required']), set(node['properties']))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(PROPOSAL_SCHEMA)
        for name in ('candidate_id', 'intent', 'order_type', 'limit_price'):
            self.assertIn({'type': 'null'}, PROPOSAL_SCHEMA['properties'][name]['anyOf'])
        valid = await ScriptedAnalyst(self.config).analyze(self.snapshot)
        self.client.responses.create.return_value = response(valid.model_dump_json())
        self.assertEqual(await self.analyst.analyze(self.snapshot), valid)
        arguments = self.client.responses.create.call_args.kwargs
        self.assertEqual(arguments['model'], 'gpt-6-astra')
        self.assertEqual(arguments['reasoning'], {'effort': 'medium'})
        self.assertEqual(arguments['tools'], [])
        self.assertIs(arguments['store'], False)
        self.assertIs(arguments['text']['format']['strict'], True)
        self.assertEqual(arguments['text']['format']['schema'], PROPOSAL_SCHEMA)
        self.assertNotIn('account', arguments['input'])
        self.assertNotIn('api_key', arguments['input'])
        self.assertEqual(self.analyst.last_metric['status'], 'ok')
        self.assertEqual(self.analyst.last_metric['total_tokens'], 170)
        await self.analyst.close()
        self.client.close.assert_awaited_once()

    async def test_invalid_outputs_unknown_references_and_unsafe_orders_hold(self):
        valid = (await ScriptedAnalyst(self.config).analyze(self.snapshot)).model_dump(mode='json')
        invalid = ['not json', '```json\n{}\n```', '{}', json.dumps({**valid, 'extra': True})]
        for changes in ({'candidate_id': 'invented'}, {'evidence_ids': ['invented']}, {'evidence_ids': []},
                        {'quantity': 2}, {'quantity': '1'}, {'order_type': 'MKT'},
                        {'intent': 'SELL_TO_OPEN'}, {'intent': 'BUY_TO_OPEN_PUT'},
                        {'valid_until': NOW.isoformat()}, {'limit_price': '0.33'}):
            invalid.append(json.dumps({**valid, **changes}))
        for value in invalid:
            with self.subTest(value=value):
                self.client.responses.create.return_value = response(value)
                result = await self.analyst.analyze(self.snapshot)
                self.assertEqual((result.action, result.quantity, result.candidate_id), ('HOLD', 0, None))
                self.assertEqual(self.analyst.last_metric['status'], 'invalid')

    async def test_refusal_incomplete_and_errors_hold_without_retry_or_error_text(self):
        text = hold(NOW, 'Nothing to do').model_dump_json()
        cases = [
            (response(text, status='incomplete'), 'incomplete'),
            (response(text, output=[SimpleNamespace(type='message', status='completed', content=[SimpleNamespace(type='refusal')])]), 'refusal'),
            (TimeoutError('sensitive timeout content'), 'timeout'),
            (RuntimeError('secret-value-from-api'), 'error'),
        ]
        for item, status in cases:
            with self.subTest(status=status):
                self.client.responses.create.reset_mock()
                self.client.responses.create.side_effect = item if isinstance(item, Exception) else None
                if not isinstance(item, Exception):
                    self.client.responses.create.return_value = item
                result = await self.analyst.analyze(self.snapshot)
                self.assertEqual(result.action, 'HOLD')
                self.assertEqual(self.analyst.last_metric['status'], status)
                self.client.responses.create.assert_awaited_once()
                self.assertNotIn('secret-value', result.model_dump_json() + json.dumps(self.analyst.last_metric))

    async def test_lazy_credentials_never_read_or_used_by_tests(self):
        with patch.dict('os.environ', {}, clear=True), patch('app.analyst.AsyncOpenAI') as factory:
            analyst = OpenAIAnalyst(self.config)
            result = await analyst.analyze(self.snapshot)
            self.assertEqual(result.action, 'HOLD')
            self.assertEqual(analyst.last_metric['status'], 'no_credentials')
            factory.assert_not_called()
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-placeholder'}, clear=True), patch('app.analyst.AsyncOpenAI', return_value=self.client) as factory:
            self.client.responses.create.return_value = response(hold(NOW, 'test').model_dump_json())
            analyst = OpenAIAnalyst(self.config)
            factory.assert_not_called()
            await analyst.analyze(self.snapshot)
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
            self.assertEqual(factory.call_args.kwargs['base_url'], 'https://api.openai.com/v1')

    async def test_scripted_closes_only_existing_long_and_no_signal_holds(self):
        scripted = ScriptedAnalyst(self.config, ['OPEN', 'CLOSE', 'HOLD'])
        self.assertEqual((await scripted.analyze(self.snapshot)).action, 'OPEN')
        self.snapshot.positions = [Position(conid=1234, symbol='AAPL', quantity=1, average_cost=.33)]
        self.assertEqual((await scripted.analyze(self.snapshot)).intent, 'SELL_TO_CLOSE')
        self.assertEqual((await scripted.analyze(self.snapshot)).action, 'HOLD')
        self.snapshot.positions = []
        self.snapshot.signal = 'NONE'
        self.assertEqual((await ScriptedAnalyst(self.config).analyze(self.snapshot)).action, 'HOLD')


if __name__ == '__main__':
    unittest.main()
