"""Full offline paper flow, persistence, data rejection and HTTP controls."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tradingagents.runtime.ai_analysis import AIAnalysis
from tradingagents.runtime.angel_data import AngelReadOnlyFeed, DemoFeed
from tradingagents.runtime.dashboard_server import DashboardServer
from tradingagents.runtime.market_snapshot import (
    IST,
    Instrument,
    InstrumentResolver,
    SessionCalendar,
    technical_snapshot,
)
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService


@pytest.fixture
def clock():
    return [datetime(2026, 9, 17, 10, 0, 30, tzinfo=IST)]


@pytest.fixture
def ledger(tmp_path):
    store = PaperLedger(tmp_path / 'account.sqlite3', initial_cash=100000)
    yield store
    store.close()


class Graph:
    def __init__(self, signal='Buy', callback=None):
        self.signal = signal
        self.callback = callback
        self.calls = []

    def propagate(self, symbol, trade_date, asset_type):
        self.calls.append((symbol, trade_date, asset_type))
        if self.callback:
            self.callback()
        return {}, self.signal


def app(ledger, clock, graph=None, feed=None, **kwargs):
    service = PaperTradingService(feed or DemoFeed(), ledger,
                                  graph_factory=(lambda: graph) if graph else None,
                                  analysis_symbol='TEST.NS', clock=lambda: clock[0], **kwargs)
    service.start(background=False)
    return service


def test_real_paper_flow_cash_positions_pnl_and_engine(ledger, clock):
    graph = Graph()
    service = app(ledger, clock, graph)
    first = service.tick()
    assert first['status'] == 'filled'
    assert first['filled_qty'] == 50
    assert ledger.account()['cash'] == 100000 - 50 * 103.5
    assert graph.calls == [('TEST.NS', '2026-09-17', 'stock')]
    assert service.tick()['reason'] == 'position_already_open'
    clock[0] += timedelta(minutes=1)
    graph.signal = 'Sell'
    assert service.tick()['status'] == 'filled'
    assert ledger.account()['positions'] == []
    assert ledger.account()['cash'] == 100000
    assert len(ledger.history()['orders']) == 2


@pytest.mark.parametrize('signal', ['Hold', 'REVIEW', 'Overweight', 'Underweight', 'unexpected'])
def test_no_orders_for_non_actionable_ratings(ledger, clock, signal):
    service = app(ledger, clock, Graph(signal))
    assert service.tick()['status'] == 'hold'
    assert not ledger.history()['orders']


def test_unconfigured_engine_monitors_without_inventing_a_signal(ledger, clock):
    service = app(ledger, clock)
    assert service.tick()['reason'] == 'engine_not_configured'
    assert not service.status()['allow_trade']
    assert not ledger.history()['orders']


def test_stop_during_analysis_prevents_commit(ledger, clock):
    graph = Graph()
    service = app(ledger, clock, graph)
    graph.callback = service.stop
    assert service.tick()['reason'] == 'stopped_during_analysis'
    assert not ledger.history()['orders']


def test_quote_expiring_during_analysis_prevents_commit(ledger, clock):
    graph = Graph(callback=lambda: clock.__setitem__(0, clock[0] + timedelta(seconds=100)))
    service = app(ledger, clock, graph)
    assert service.tick()['status'] == 'error'
    assert not ledger.history()['orders']
    assert not service.status()['allow_trade']


def test_kill_and_duplicate_protection_survive_restart(tmp_path, clock):
    path = tmp_path / 'persistent.sqlite3'
    store = PaperLedger(path)
    instrument = DemoFeed().instrument
    result = store.execute(instrument, 'buy', 1, 100, dedupe='same', stamp=clock[0].isoformat())
    store.kill(True, clock[0].isoformat())
    store.close()
    reopened = PaperLedger(path)
    try:
        assert reopened.killed()
        assert reopened.execute(instrument, 'buy', 1, 100, dedupe='same',
                                stamp=clock[0].isoformat())['order_id'] == result['order_id']
        service = PaperTradingService(DemoFeed(), reopened, clock=lambda: clock[0])
        with pytest.raises(ValueError, match='latched'):
            service.start(background=False)
        service.reset_stop()
        service.start(background=False)
        assert len(reopened.history()['orders']) == 1
    finally:
        reopened.close()


def test_concurrent_duplicate_fills_are_atomic(ledger, clock):
    results = []
    def execute():
        results.append(ledger.execute(DemoFeed().instrument, 'buy', 1, 100,
                                      dedupe='one', stamp=clock[0].isoformat()))
    workers = [threading.Thread(target=execute) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sum(r['status'] == 'filled' for r in results) == 1
    assert ledger.account()['positions'][0]['qty'] == 1


def test_margin_short_and_lot_guards(ledger, clock):
    stamp = clock[0].isoformat()
    instrument = DemoFeed().instrument
    assert ledger.execute(instrument, 'buy', 1001, 100, dedupe='large', stamp=stamp)['reason'] == 'insufficient_paper_margin'
    assert ledger.execute(instrument, 'sell', 1, 100, dedupe='short', stamp=stamp)['reason'] == 'short_selling_not_supported'
    derivative = Instrument('NFO', 'TEST-FUT', '123', 25, 'FUTIDX', '2026-09-30')
    with pytest.raises(ValueError, match='lot_multiple'):
        ledger.execute(derivative, 'buy', 1, 100, dedupe='lot', stamp=stamp)
    assert ledger.account()['cash'] == 100000


def test_realized_unrealized_and_daily_loss_math(ledger, clock):
    instrument = DemoFeed().instrument
    ledger.account(day='2026-09-17')
    ledger.execute(instrument, 'buy', 10, 100, dedupe='buy', stamp=clock[0].isoformat())
    account = ledger.account(mark=(instrument.key, 90))
    assert account['daily_pnl'] == -100
    assert account['unrealized_pnl'] == -100
    ledger.execute(instrument, 'sell', 10, 90, dedupe='sell', stamp=clock[0].isoformat())
    assert ledger.account()['realized_pnl'] == -100
    assert ledger.account()['equity'] == 99900


class FailingFeed(DemoFeed):
    def fetch(self, now):
        raise RuntimeError('SECRET_KEY_MUST_NOT_APPEAR')


def test_errors_stop_after_bounded_attempts_without_leaking_details(ledger, clock):
    service = app(ledger, clock, feed=FailingFeed(), max_errors=2)
    service.tick()
    service.tick()
    state = service.status()
    assert state['status'] == 'stopped'
    assert state['error_count'] == 2
    assert 'SECRET_KEY' not in json.dumps(state)


def test_background_scheduler_executes_and_stops(ledger, clock):
    observed = threading.Event()
    class Feed(DemoFeed):
        def fetch(self, now):
            observed.set()
            return super().fetch(now)
    service = PaperTradingService(Feed(), ledger, interval=1, clock=lambda: clock[0])
    service.start()
    assert observed.wait(2)
    service.stop()
    service.thread.join(2)
    assert not service.thread.is_alive()
    assert service.tick()['reason'] == 'stopped'


class RealSource(DemoFeed):
    source = 'angel'
    def fetch(self, now):
        result = super().fetch(now)
        result['source'] = 'angel'
        return result


def test_market_close_and_holiday_block_fetches(ledger, clock):
    calendar = SessionCalendar({'year': 2026, 'holidays': ['2026-09-17']})
    service = app(ledger, clock, Graph(), RealSource(), calendar=calendar)
    assert service.tick()['status'] == 'market_closed'
    assert service.last_snapshot is None


def test_closing_window_flattens_paper_position(ledger, clock):
    calendar = SessionCalendar({'year': 2026})
    service = app(ledger, clock, Graph(), RealSource(), calendar=calendar)
    assert service.tick()['status'] == 'filled'
    clock[0] = clock[0].replace(hour=15, minute=26)
    assert service.tick()['status'] == 'filled'
    assert ledger.account()['positions'] == []


def test_current_calendar_required_for_real_feed(ledger, clock):
    service = PaperTradingService(RealSource(), ledger, clock=lambda: clock[0])
    with pytest.raises(ValueError, match='calendar_required'):
        service.start(background=False)


def test_calendar_honors_special_weekend_session():
    calendar = SessionCalendar({'year': 2026, 'special_sessions': {
        '2026-09-19': {'open': '18:00', 'close': '19:00'}}})
    assert calendar.is_open(datetime(2026, 9, 19, 18, 30, tzinfo=IST))
    assert not calendar.is_open(datetime(2026, 9, 19, 10, 0, tzinfo=IST))


def test_resolver_rejects_ambiguity_and_index_trading(clock):
    rows = [{'exch_seg': 'NSE', 'symbol': 'TEST-EQ', 'token': '123', 'lotsize': '1'}]
    assert InstrumentResolver(rows).resolve('NSE', 'TEST-EQ').token == '123'
    with pytest.raises(ValueError, match='ambiguous'):
        InstrumentResolver(rows * 2).resolve('NSE', 'TEST-EQ')
    with pytest.raises(ValueError, match='non_tradable'):
        Instrument('NSE', 'NIFTY 50', '99926000', instrument_type='INDEX').validate_trade(clock[0])


def candles(now):
    return [[(now.replace(second=0) - timedelta(minutes=35-i)).isoformat(),
             100+i, 102+i, 99+i, 101+i, 1000] for i in range(35)]


def test_candle_metrics_require_complete_recent_contiguous_data(clock):
    rows = candles(clock[0])
    metrics = technical_snapshot(rows, clock[0])
    assert metrics['atr'] == 3
    assert metrics['trend_strength'] == 1
    assert metrics['atr_ratio'] == 1
    with pytest.raises(ValueError, match='insufficient'):
        technical_snapshot(rows[:10], clock[0])
    with pytest.raises(ValueError, match='stale'):
        technical_snapshot(rows, clock[0] + timedelta(minutes=5))
    rows.pop(-5)
    with pytest.raises(ValueError, match='gap'):
        technical_snapshot(rows, clock[0])


class FakeAngel:
    def __init__(self, now):
        self.now = now
    def getMarketData(self, mode, tokens):
        return {'status': True, 'data': {'fetched': [
            {'symbolToken': token, 'exchange': 'NSE', 'ltp': value,
             'exchFeedTime': self.now.isoformat()} for token, value in [('123', 134), ('456', 18)]],
             'unfetched': []}}
    def getCandleData(self, request):
        assert request['interval'] == 'ONE_MINUTE'
        return {'status': True, 'data': candles(self.now)}
    def rmsLimit(self):
        return {'status': True, 'data': {'availablecash': '1000'}}
    def position(self):
        return {'status': True, 'data': []}
    def orderBook(self):
        return {'status': True, 'data': [{'orderid': '1', 'status': 'open',
                                        'filledshares': '2', 'unfilledshares': '3', 'jwtToken': 'SECRET'}]}


def test_angel_feed_normalizes_read_only_data_and_preserves_partial_status(clock):
    feed = AngelReadOnlyFeed(Instrument('NSE', 'TEST-EQ', '123'),
                            Instrument('NSE', 'India VIX', '456', instrument_type='INDEX'))
    feed.client = FakeAngel(clock[0])
    data = feed.fetch(clock[0])
    assert data['vix'] == 18
    assert data['broker_account']['available_cash'] == 1000
    assert data['broker_account']['orders'][0]['status'] == 'open'
    assert data['broker_account']['orders'][0]['filledshares'] == '2'
    assert 'SECRET' not in json.dumps(data)


def test_http_ui_and_csrf_protected_controls(ledger, clock, monkeypatch):
    service = PaperTradingService(DemoFeed(), ledger, clock=lambda: clock[0])
    server = DashboardServer(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(server.origin, timeout=3) as response:
            html = response.read().decode()
            assert 'PAPER ACCOUNT' in html
            assert server.control_token in html
        with urlopen(server.origin + '/api/status', timeout=3) as response:
            assert json.load(response)['status'] == 'stopped'
        def post(action, token, origin=None):
            headers = {'Content-Type': 'application/json', 'X-Control-Token': token}
            if origin:
                headers['Origin'] = origin
            return urlopen(Request(server.origin + '/api/' + action, data=b'{}', headers=headers), timeout=3)
        with pytest.raises(HTTPError) as error:
            post('start', 'wrong')
        assert error.value.code == 403
        with pytest.raises(HTTPError):
            post('start', server.control_token, 'https://example.test')
        with post('start', server.control_token) as response:
            assert json.load(response)['status'] == 'running'
        with post('kill', server.control_token) as response:
            assert json.load(response)['account']['kill_switch']
        with pytest.raises(HTTPError) as error:
            post('start', server.control_token)
        assert error.value.code == 409
        # Reset kill switch for further testing
        with post('reset', server.control_token) as response:
            assert not json.load(response)['account']['kill_switch']
        # Whitelisted public origin succeeds
        monkeypatch.setenv('TRADINGAGENTS_ALLOWED_ORIGINS', 'http://139.59.89.238')
        with post('start', server.control_token, 'http://139.59.89.238') as response:
            assert json.load(response)['status'] == 'running'
        # Untrusted origin is rejected with 403 Forbidden
        with pytest.raises(HTTPError) as error:
            post('stop', server.control_token, 'http://malicious.evil.com')
        assert error.value.code == 403
    finally:
        service.stop()
        if service.thread:
            service.thread.join(2)
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('field,value', [('price', float('nan')), ('vix', 0),
                                        ('timestamp', '2026-09-16T10:00:00+05:30'),
                                        ('vix_timestamp', '2026-09-16T10:00:00+05:30')])
def test_bad_market_inputs_block_before_engine(ledger, clock, field, value):
    class BadFeed(DemoFeed):
        def fetch(self, now):
            result = super().fetch(now)
            result[field] = value
            return result
    graph = Graph()
    service = app(ledger, clock, graph, BadFeed())
    assert service.tick()['status'] == 'error'
    assert not graph.calls
    assert not ledger.history()['orders']


def test_data_identity_cannot_change_traded_instrument(ledger, clock):
    class BadFeed(DemoFeed):
        def fetch(self, now):
            result = super().fetch(now)
            result['instrument']['token'] = '999'
            return result
    service = app(ledger, clock, Graph(), BadFeed())
    assert service.tick()['reason'] == 'snapshot_identity_mismatch'
    assert not ledger.history()['orders']


def test_shutdown_keeps_existing_positions_and_blocks_new_orders(ledger, clock):
    service = app(ledger, clock, Graph())
    service.tick()
    status = service.emergency_stop()
    assert status['account']['positions'][0]['qty'] == 50
    assert status['last_result']['positions_closed'] is False
    assert not status['allow_trade']
    assert service.tick()['status'] == 'blocked'


def test_close_window_rechecked_after_slow_analysis(ledger, clock):
    clock[0] = clock[0].replace(hour=15, minute=24, second=50)
    graph = Graph(callback=lambda: clock.__setitem__(0, clock[0] + timedelta(seconds=20)))
    service = app(ledger, clock, graph, RealSource(), calendar=SessionCalendar({'year': 2026}))
    assert service.tick()['reason'] == 'closing_window'
    assert not ledger.history()['orders']


def test_angel_config_is_not_reported_authenticated():
    from tradingagents.runtime.broker_adapter import (
        BrokerAdapterConfig,
        connect_broker,
        create_broker_adapter,
    )
    state = connect_broker('angel', api_key='key', client_id='client', mpin='pin', totp_secret='secret')
    assert state['status'] == 'configured'
    assert state['reason'] == 'broker_authentication_required'
    with pytest.raises(ValueError, match='unsupported_broker'):
        create_broker_adapter(BrokerAdapterConfig(broker_name='typo'))


@pytest.mark.parametrize('qty,price', [(0, 100), (-1, 100), (1.5, 100), (True, 100),
                                      (1, float('nan')), (1, float('inf')), (1, -100)])
def test_direct_paper_paths_cannot_fake_invalid_fills(qty, price):
    from tradingagents.runtime.broker_adapter import PaperBrokerAdapter
    from tradingagents.runtime.order_gateway import MarketOrderGateway
    for adapter in (PaperBrokerAdapter(), MarketOrderGateway('paper')):
        result = adapter.place_order({'symbol': 'TEST', 'side': 'buy', 'qty': qty, 'price': price})
        assert result['status'] == 'rejected'
        assert 'fills' not in result


def test_api_auto_start_and_stop(ledger, clock):
    calendar = SessionCalendar({'year': 2026})
    service = PaperTradingService(RealSource(), ledger, calendar=calendar, clock=lambda: clock[0])
    server = DashboardServer(service, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def post(endpoint, data):
            headers = {'Content-Type': 'application/json', 'X-Control-Token': server.control_token}
            req = Request(f'{server.origin}/api/{endpoint}', data=json.dumps(data).encode(), headers=headers)
            with urlopen(req, timeout=3) as resp:
                return json.load(resp)

        # Initial state is paused
        status = service.status()
        assert not status['auto_enabled']
        assert status['status'] == 'stopped'

        # Auto start with correct symbol
        res = post('auto/start', {'symbol': 'DEMO.NS'})
        assert res['auto_enabled'] is True
        assert res['status'] == 'running'
        assert service.auto_enabled is True
        assert service.running is True

        # Stop resets both running and auto_enabled
        res_stop = post('stop', {})
        assert res_stop['auto_enabled'] is False
        assert res_stop['status'] == 'stopped'
        assert service.auto_enabled is False
        assert service.running is False
    finally:
        service.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ai_disconnect_and_reconnect_from_env(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-google-key-secure')
    ai = AIAnalysis()
    assert ai.state == 'ready'
    assert ai.config['llm_provider'] == 'google'

    # Disconnect clears credentials and state
    ai.disconnect()
    assert ai.state == 'not_configured'
    assert ai.config is None

    # Empty body configure restores from env
    ai.configure({})
    assert ai.state == 'ready'
    assert ai.config['llm_provider'] == 'google'

    # Disconnect and configure with provider/model but blank api_key restores key from env
    ai.disconnect()
    ai.configure({'provider': 'google', 'model': 'gemini-3.6-flash'})
    assert ai.state == 'ready'
    assert ai.config['deep_think_llm'] == 'gemini-3.6-flash'

    # Placeholder key is rejected
    ai.disconnect()
    with pytest.raises(ValueError, match='api_key_required'):
        ai.configure({'provider': 'google', 'model': 'gemini-3.6-flash', 'api_key': 'placeholder'})


def test_angel_order_execution_apis_hard_blocked():
    from tradingagents.integrations.angel_one.client import AngelOneClient

    client = AngelOneClient('fake_api_key', 'fake_client_id', '1234', 'fake_totp')
    with pytest.raises(NotImplementedError, match='strictly prohibited'):
        client.place_order(symbol='SBIN', qty=1)
    with pytest.raises(NotImplementedError, match='strictly prohibited'):
        client.modify_order(order_id='123')
    with pytest.raises(NotImplementedError, match='strictly prohibited'):
        client.cancel_order(order_id='123')


def test_main_auto_start_logic(tmp_path, monkeypatch):
    from unittest.mock import patch

    from tradingagents.runtime.__main__ import main
    from tradingagents.runtime.paper_ledger import PaperLedger

    db_path = tmp_path / 'test_main.sqlite3'

    # 1. Default without flag: starts paused
    with patch('tradingagents.runtime.__main__.DashboardServer') as mock_server_cls:
        mock_instance = mock_server_cls.return_value
        mock_instance.origin = 'http://127.0.0.1:8765'
        default_state = {}

        def check_default(*args, **kwargs):
            svc = mock_server_cls.call_args[0][0]
            default_state['running'] = svc.running
            default_state['auto_enabled'] = svc.auto_enabled
            raise KeyboardInterrupt()

        mock_instance.serve_forever.side_effect = check_default

        main(['--db', str(db_path), '--source', 'demo'])
        assert default_state['running'] is False
        assert default_state['auto_enabled'] is False

    # 2. With TRADINGAGENTS_AUTO_START=1: auto-starts demo
    monkeypatch.setenv('TRADINGAGENTS_AUTO_START', '1')
    with patch('tradingagents.runtime.__main__.DashboardServer') as mock_server_cls:
        mock_instance = mock_server_cls.return_value
        mock_instance.origin = 'http://127.0.0.1:8765'
        auto_state = {}

        def check_auto(*args, **kwargs):
            svc = mock_server_cls.call_args[0][0]
            auto_state['running'] = svc.running
            raise KeyboardInterrupt()

        mock_instance.serve_forever.side_effect = check_auto

        main(['--db', str(db_path), '--source', 'demo'])
        assert auto_state['running'] is True

    # 3. With CLI --auto-start and angel source
    monkeypatch.delenv('TRADINGAGENTS_AUTO_START', raising=False)
    master_file = tmp_path / 'master.json'
    calendar_file = tmp_path / 'calendar.json'
    master_file.write_text(json.dumps([
        {'exch_seg': 'NSE', 'symbol': 'SBIN-EQ', 'token': '3045', 'lotsize': 1, 'instrumenttype': 'EQ'},
        {'exch_seg': 'NSE', 'symbol': 'INDIA_VIX', 'token': '999', 'lotsize': 1, 'instrumenttype': 'INDEX'},
    ]), encoding='utf-8')
    calendar_file.write_text(json.dumps({'year': 2026}), encoding='utf-8')

    with patch('tradingagents.runtime.__main__.DashboardServer') as mock_server_cls, \
         patch('tradingagents.runtime.__main__.AngelReadOnlyFeed') as mock_feed_cls:
        from tradingagents.runtime.market_snapshot import Instrument
        mock_feed = mock_feed_cls.return_value
        mock_feed.source = 'angel'
        mock_feed.instrument = Instrument('NSE', 'SBIN-EQ', '3045', 1, 'EQ')
        mock_feed.connected = True
        mock_instance = mock_server_cls.return_value
        mock_instance.origin = 'http://127.0.0.1:8765'
        angel_state = {}

        def check_angel(*args, **kwargs):
            svc = mock_server_cls.call_args[0][0]
            angel_state['running'] = svc.running
            angel_state['auto_enabled'] = svc.auto_enabled
            angel_state['symbol'] = svc.analysis_symbol
            raise KeyboardInterrupt()

        mock_instance.serve_forever.side_effect = check_angel

        db_angel = tmp_path / 'test_angel.sqlite3'
        main([
            '--db', str(db_angel),
            '--source', 'angel',
            '--auto-start',
            '--master', str(master_file),
            '--calendar', str(calendar_file),
            '--symbol', 'SBIN-EQ',
            '--vix-symbol', 'INDIA_VIX',
        ])
        assert angel_state['running'] is True
        assert angel_state['auto_enabled'] is True
        assert angel_state['symbol'] == 'SBIN.NS'

    # 4. With emergency stop latched: cannot auto-start even with TRADINGAGENTS_AUTO_START=1
    monkeypatch.setenv('TRADINGAGENTS_AUTO_START', '1')
    ledger = PaperLedger(db_path)
    ledger.kill(True, '2026-09-17T10:00:00+05:30')
    ledger.close()

    with patch('tradingagents.runtime.__main__.DashboardServer') as mock_server_cls:
        mock_instance = mock_server_cls.return_value
        mock_instance.origin = 'http://127.0.0.1:8765'
        latched_state = {}

        def check_latched(*args, **kwargs):
            svc = mock_server_cls.call_args[0][0]
            latched_state['running'] = svc.running
            raise KeyboardInterrupt()

        mock_instance.serve_forever.side_effect = check_latched

        main(['--db', str(db_path), '--source', 'demo'])
        assert latched_state['running'] is False




