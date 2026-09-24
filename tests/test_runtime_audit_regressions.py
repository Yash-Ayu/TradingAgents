"""Regression coverage for audited runtime execution and date boundaries."""
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from tradingagents.runtime.browser_dashboard import BrowserDashboard
from tradingagents.runtime.engine_bridge import SafeTradingEngineBridge, resolve_trade_date
from tradingagents.runtime.market_session import MarketSessionMonitor
from tradingagents.runtime.market_trigger import MarketTriggerLoop
from tradingagents.runtime.safe_runtime import RiskGate, SafeTradingRuntime
from tradingagents.runtime.trade_router import TradeRouter


@pytest.fixture
def snapshot():
    return {"is_market_open": True, "vix": 18, "drawdown_pct": 3,
            "trend_strength": 0.8, "atr_ratio": 0.7}


def router_for(signal):
    graph = MagicMock()
    graph.propagate.return_value = ({}, signal)
    router = TradeRouter('paper', bridge=SafeTradingEngineBridge(lambda: graph))
    return router, graph


@pytest.mark.parametrize('signal', ['Hold', 'REVIEW', 'Overweight', 'Underweight',
                                    'ENGINE_NOT_CONFIGURED', None, 'do not BUY', 'STRONG BUY'])
def test_non_actionable_decisions_never_reach_gateway(snapshot, signal):
    router, _ = router_for(signal)
    router.gateway.place_order = MagicMock()
    snapshot['order'] = {"qty": 1, "price": 100}
    assert router.route(snapshot)['reason'] == 'no_actionable_engine_decision'
    router.gateway.place_order.assert_not_called()


@pytest.mark.parametrize('signal, side', [('Buy', 'buy'), ('Sell', 'sell')])
def test_explicit_paper_order_uses_engine_direction_and_selected_date(snapshot, signal, side):
    router, graph = router_for(signal)
    snapshot.update(trade_date='2026-09-01', asset_type='crypto',
                    order={"qty": 2, "price": 100, "side": "incorrect", "risk_approved": False})
    result = router.route(snapshot, symbol='BTC-USD')
    assert result['status'] == 'accepted'
    assert result['order_id']
    assert result['fills'][0]['side'] == side
    assert result['fills'][0]['qty'] == 2
    graph.propagate.assert_called_once_with('BTC-USD', '2026-09-01', asset_type='crypto')


def test_engine_decision_does_not_invent_quantity_or_price(snapshot):
    router, _ = router_for('Buy')
    router.gateway.place_order = MagicMock()
    assert router.route(snapshot)['reason'] == 'explicit_order_required'
    router.gateway.place_order.assert_not_called()


@pytest.mark.parametrize('field, value', [
    ('qty', -1), ('qty', 1.5), ('qty', True), ('qty', 'nan'),
    ('qty', float('inf')), ('price', -1), ('price', float('nan')),
    ('price', float('inf')), ('price', True), ('symbol', 'OTHER'),
])
def test_invalid_orders_never_reach_gateway(snapshot, field, value):
    router, _ = router_for('Sell')
    router.gateway.place_order = MagicMock()
    snapshot['order'] = {"qty": 1, "price": 100}
    snapshot['order'][field] = value
    assert router.route(snapshot)['status'] == 'blocked'
    router.gateway.place_order.assert_not_called()


@pytest.mark.parametrize('value', ['', '2026-02-30', '20260901', '01/09/2026', 20260901,
                                 '2099-01-01'])
def test_invalid_dates_block_before_engine_or_gateway(snapshot, value):
    router, graph = router_for('Buy')
    snapshot['trade_date'] = value
    assert router.route(snapshot)['status'] == 'blocked'
    graph.propagate.assert_not_called()
    assert MarketTriggerLoop().evaluate(snapshot)['allowed'] is False


def test_live_historical_analysis_cannot_reach_broker(snapshot):
    router, graph = router_for('Buy')
    router.gateway.broker_name = 'angel'
    router.gateway.place_order = MagicMock()
    snapshot.update(trade_date='2025-01-01', order={"qty": 1, "price": 100})
    assert router.route(snapshot)['reason'] == 'historical_date_not_allowed_for_live_orders'
    graph.propagate.assert_not_called()
    router.gateway.place_order.assert_not_called()


def test_default_date_uses_indian_timezone():
    instant = datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc)
    assert resolve_trade_date({}, now=instant) == '2026-09-17'
    assert resolve_trade_date({'trade_date': '2026-09-01'}, now=instant) == '2026-09-01'


def test_trigger_passes_historical_date_as_date_not_asset_type(snapshot):
    graph = MagicMock()
    graph.propagate.return_value = ({}, 'Hold')
    trigger = MarketTriggerLoop(SafeTradingEngineBridge(lambda: graph))
    snapshot['trade_date'] = '2026-09-01'
    trigger.evaluate(snapshot)
    graph.propagate.assert_called_once_with('NIFTY', '2026-09-01', asset_type='stock')


@pytest.mark.parametrize('field', ['vix', 'drawdown_pct', 'trend_strength', 'atr_ratio'])
@pytest.mark.parametrize('value', [None, 'invalid', float('nan'), float('inf'), -1, True])
def test_invalid_risk_data_cannot_authorize_trade(snapshot, field, value):
    snapshot[field] = value
    assert RiskGate().evaluate(snapshot)['allow_trade'] is False


def test_missing_snapshot_and_stopped_runtime_cannot_authorize(snapshot):
    assert not RiskGate().should_allow_trade({})
    runtime = SafeTradingRuntime()
    runtime.start()
    assert not runtime.can_trade()
    assert runtime.can_trade(snapshot)
    runtime.stop()
    assert not runtime.can_trade(snapshot)
    assert not runtime.get_status()['allow_trade']


def test_session_handles_weekends_holidays_and_utc():
    monitor = MarketSessionMonitor(holidays={date(2026, 9, 11)})
    assert not monitor.is_market_open(datetime(2026, 9, 11, 10))
    assert not monitor.is_market_open(datetime(2026, 9, 12, 10))
    assert monitor.is_market_open(datetime(2026, 9, 14, 4, tzinfo=timezone.utc))
    assert not monitor.is_market_open(datetime(2026, 9, 14, 10, tzinfo=timezone.utc))


def test_dashboard_escapes_external_text_and_defaults_to_stopped():
    assert BrowserDashboard().get_status()['allow_trade'] is False
    rendered = BrowserDashboard({'<script>': '<img src=x onerror=alert(1)>'}).render()
    assert '<script>' not in rendered
    assert '<img' not in rendered
