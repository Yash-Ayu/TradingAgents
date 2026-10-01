"""Focused regression tests for discovery-only F&O scanning during RiskGate block."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.integrations.angel_one.models import DerivativeType, MarketBias
from tradingagents.runtime.fo_scanner import ScanCandidate, ScanEvaluationResult, SetupState
from tradingagents.runtime.market_snapshot import IST, Instrument, SessionCalendar
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService


def _generate_valid_candles(base_price=1000.0, n_bars=30, base_time=None):
    rows = []
    start = base_time or datetime.now(IST) - timedelta(minutes=5 * n_bars)
    for i in range(n_bars):
        t = (start + timedelta(minutes=5 * i)).isoformat()
        bar_o = base_price + (i % 3) * 2.0
        bar_h = bar_o + 5.0
        bar_l = bar_o - 5.0
        bar_c = bar_o + 1.0
        rows.append([t, bar_o, bar_h, bar_l, bar_c, 5000])
    return rows


class MockScripMaster:
    def __init__(self):
        self._universe = {
            "RELIANCE": {"underlying": "RELIANCE", "exchange": "NFO", "is_index": False, "lot_size": 250, "expiries": ["26SEP2026"]},
            "NIFTY": {"underlying": "NIFTY", "exchange": "NFO", "is_index": True, "lot_size": 25, "expiries": ["26SEP2026"]},
        }

    def get_fo_universe(self, exchange: str = "NFO"):
        return dict(self._universe)

    def discover_universe(self):
        return ["NIFTY", "RELIANCE"]


def _make_candidate(symbol="RELIANCE", spot_price=2500.0):
    return ScanCandidate(
        symbol=symbol,
        underlying=symbol,
        is_index=False,
        instrument_type=DerivativeType.OPTSTK,
        bias=MarketBias.BULLISH,
        action="CE_BUY",
        spot_price=spot_price,
        entry_price=spot_price,
        stop_loss=spot_price - 20.0,
        target=spot_price + 40.0,
        risk_reward_ratio=2.0,
        setup_name="TEST_BREAKOUT",
        state=SetupState.TRIGGERED,
        reason="Breakout detected",
    )


def _make_service(tmp_path, vix=14.0, drawdown_pct=0.0, clock_time=None, source="demo", **kwargs):
    ledger = PaperLedger(tmp_path / f"ledger_{uuid.uuid4().hex[:8]}.sqlite3", initial_cash=500000.0)
    current_time = clock_time or datetime(2026, 9, 17, 10, 0, 0, tzinfo=IST)
    bar_start = (current_time.replace(second=0, microsecond=0) - timedelta(minutes=1)).isoformat()

    inst = Instrument("NSE", "Nifty 50", "99926000", lot_size=25, instrument_type="INDEX")
    feed = MagicMock()
    feed.source = source
    feed.instrument = inst
    feed.connected = True

    snapshot = {
        'source': source,
        'instrument': inst.as_dict(),
        'price': 25000.0,
        'vix': vix,
        'atr': 120.0,
        'atr_ratio': 1.0,
        'trend_strength': 0.8,
        'timestamp': current_time.isoformat(),
        'vix_timestamp': current_time.isoformat(),
        'bar_timestamp': bar_start,
        'chart': _generate_valid_candles(base_price=25000.0, n_bars=30, base_time=current_time - timedelta(minutes=150))
    }
    feed.snapshot.return_value = snapshot
    feed.fetch.return_value = snapshot

    service = PaperTradingService(
        feed=feed,
        ledger=ledger,
        scrip_master=MockScripMaster(),
        scanner_batch_size=5,
        clock=lambda: current_time,
        calendar=SessionCalendar({'year': 2026}),
        **kwargs
    )
    service.start(background=False)
    return service, feed, ledger, current_time


def test_a_risk_blocked_cycle_runs_discovery_without_execution(tmp_path):
    """Test A: When RiskGate blocks trading, scanner runs discovery, updates telemetry, but NEVER executes."""
    service, feed, ledger, current_time = _make_service(tmp_path, vix=42.0)
    service.auto_enabled = True

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {
        'chart': _generate_valid_candles(2500.0, 30, base_time=current_time - timedelta(minutes=150)),
        'source': 'DEMO_SYNTHETIC'
    }

    candidate = _make_candidate("RELIANCE", 2500.0)
    eval_res = ScanEvaluationResult(
        symbol="RELIANCE",
        underlying="RELIANCE",
        spot_price=2500.0,
        state=SetupState.TRIGGERED,
        bias=MarketBias.BULLISH,
        reason="Breakout detected",
        candidate=candidate,
    )

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter), \
         patch.object(service.fo_scanner, 'evaluate_price_action', return_value=eval_res), \
         patch.object(service.fo_orchestrator, 'process_signal') as mock_process:
        result = service.tick()

    # 1. Result must remain risk_blocked with original reason
    assert result['status'] == 'risk_blocked'
    assert any('VIX' in r for r in result['reasons'])

    # 2. Scanner executed and updated telemetry
    assert service.last_scanner_result['universe_count'] == 2
    assert service.last_scanner_result['screened_count'] >= 1
    assert len(service.last_scanner_result['active_candidates']) >= 1
    assert service.last_scanner_result['execution_allowed'] is False

    # 3. Execution was strictly blocked: process_signal NEVER called, no paper positions
    assert mock_process.call_count == 0
    assert len(service.fo_orchestrator.paper_engine.get_open_positions()) == 0


def test_b_allow_execution_false_directly_blocks_btst_and_intraday(tmp_path):
    """Test B: allow_execution=False directly blocks both BTST and Intraday process_signal calls."""
    service, feed, ledger, current_time = _make_service(tmp_path, vix=14.0)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {
        'chart': _generate_valid_candles(2500.0, 30, base_time=current_time - timedelta(minutes=150)),
        'source': 'DEMO_SYNTHETIC'
    }

    # B1: Intraday candidate with allow_execution=False
    intraday_candidate = _make_candidate("RELIANCE", 2500.0)
    eval_res = ScanEvaluationResult(
        symbol="RELIANCE",
        underlying="RELIANCE",
        spot_price=2500.0,
        state=SetupState.TRIGGERED,
        bias=MarketBias.BULLISH,
        reason="Breakout detected",
        candidate=intraday_candidate,
    )

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter), \
         patch.object(service.fo_scanner, 'evaluate_price_action', return_value=eval_res), \
         patch.object(service.fo_orchestrator, 'process_signal') as mock_process:
        exec_res = service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1, allow_execution=False)

    assert exec_res is None
    assert mock_process.call_count == 0
    assert len(service.last_scanner_result['active_candidates']) >= 1
    assert service.last_scanner_result['execution_allowed'] is False

    # B2: BTST candidate in window with allow_execution=False
    btst_cand = MagicMock()
    btst_cand.spot_price = 2500.0
    btst_cand.bias = MarketBias.BULLISH
    btst_cand.stop_loss = 2470.0
    btst_cand.target = 2560.0
    btst_cand.strategy_id = "BTST_MOMENTUM"
    btst_eval_res = MagicMock()
    btst_eval_res.candidate = btst_cand

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter), \
         patch.object(service.btst_engine, 'is_in_btst_window', return_value=(True, "Active")), \
         patch.object(service.btst_engine, 'evaluate_btst_candidate', return_value=btst_eval_res), \
         patch.object(service.fo_orchestrator, 'process_signal') as mock_process:
        exec_res = service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1, allow_execution=False)

    assert exec_res is None
    assert mock_process.call_count == 0
    assert len(service.last_btst_result['candidates']) >= 1


def test_c_normal_risk_allowed_execution_path_behaves_as_before(tmp_path):
    """Test C: Normal risk-allowed execution path calls process_signal as before."""
    service, feed, ledger, current_time = _make_service(tmp_path, vix=14.0)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {
        'chart': _generate_valid_candles(2500.0, 30, base_time=current_time - timedelta(minutes=150)),
        'source': 'DEMO_SYNTHETIC'
    }

    intraday_candidate = _make_candidate("RELIANCE", 2500.0)
    eval_res = ScanEvaluationResult(
        symbol="RELIANCE",
        underlying="RELIANCE",
        spot_price=2500.0,
        state=SetupState.TRIGGERED,
        bias=MarketBias.BULLISH,
        reason="Breakout detected",
        candidate=intraday_candidate,
    )

    pipe_res = MagicMock()
    pipe_res.success = True
    pipe_res.contract.trading_symbol = "RELIANCE26SEP2500CE"

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter), \
         patch.object(service.fo_scanner, 'evaluate_price_action', return_value=eval_res), \
         patch.object(service.fo_orchestrator, 'process_signal', return_value=pipe_res) as mock_process:
        exec_res = service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1, allow_execution=True)

    assert mock_process.call_count == 1
    assert exec_res is not None
    assert exec_res['status'] == 'executed_intraday_fo_paper'
    assert service.last_scanner_result['execution_allowed'] is True


def test_d_engine_graph_factory_mode_runs_discovery_on_risk_blocked_cycle(tmp_path):
    """Test D: --engine / graph_factory mode allows discovery-only scanning on risk-blocked cycle."""
    mock_graph = MagicMock()
    service, feed, ledger, current_time = _make_service(
        tmp_path,
        vix=45.0,
        graph_factory=lambda: mock_graph
    )
    # auto_enabled must remain False in CLI engine mode
    assert service.auto_enabled is False
    assert service.graph_factory is not None

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {
        'chart': _generate_valid_candles(2500.0, 30, base_time=current_time - timedelta(minutes=150)),
        'source': 'DEMO_SYNTHETIC'
    }

    candidate = _make_candidate("RELIANCE", 2500.0)
    eval_res = ScanEvaluationResult(
        symbol="RELIANCE",
        underlying="RELIANCE",
        spot_price=2500.0,
        state=SetupState.TRIGGERED,
        bias=MarketBias.BULLISH,
        reason="Breakout detected",
        candidate=candidate,
    )

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter), \
         patch.object(service.fo_scanner, 'evaluate_price_action', return_value=eval_res), \
         patch.object(service.fo_orchestrator, 'process_signal') as mock_process:
        result = service.tick()

    assert result['status'] == 'risk_blocked'
    assert service.last_scanner_result['screened_count'] >= 1
    assert len(service.last_scanner_result['active_candidates']) >= 1
    assert service.last_scanner_result['execution_allowed'] is False
    assert mock_process.call_count == 0
    # AI auto mode must NOT be accidentally enabled
    assert service.auto_enabled is False


def test_e_protective_exits_daily_loss_closing_window_emergency_stop_preserved(tmp_path):
    """Test E: Protective exits, daily-loss kill/flatten, closing-window, and emergency-stop remain intact."""
    # 1. Emergency stop
    service, feed, ledger, current_time = _make_service(tmp_path, vix=14.0)
    service.emergency_stop()
    assert service.tick()['status'] == 'blocked'

    # 2. Closing window (within 5 min of 15:30 close on real calendar)
    closing_time = datetime(2026, 9, 17, 15, 27, 0, tzinfo=IST)
    service_closing, _, _, _ = _make_service(tmp_path, vix=14.0, clock_time=closing_time, source="angel")
    close_res = service_closing.tick()
    assert close_res['status'] == 'closing_window'
    assert service_closing.last_scanner_result['universe_count'] == 0

    # 3. Daily loss limit reached
    service_loss, feed_loss, ledger_loss, _ = _make_service(tmp_path, vix=14.0, daily_loss_limit=3.0)
    with patch.object(ledger_loss, 'account', return_value={
        'daily_loss_pct': 5.0,
        'drawdown_pct': 5.0,
        'cash': 400000.0,
        'positions': []
    }):
        loss_res = service_loss.tick()
        assert loss_res['status'] == 'risk_blocked'
        assert any('Daily paper loss limit reached' in r for r in loss_res['reasons'])
