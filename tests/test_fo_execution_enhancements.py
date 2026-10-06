"""Comprehensive tests for F&O execution enhancements, transient error resilience,
option SL/Target normalization, and mark sync exit triggers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.integrations.angel_one.models import (
    ContractSpec,
    DerivativeType,
    Exchange,
    MarketBias,
    OptionType,
)
from tradingagents.integrations.angel_one.orchestrator import FOPipelineOrchestrator
from tradingagents.integrations.angel_one.paper_engine import FOPaperTradingEngine
from tradingagents.integrations.angel_one.risk_engine import (
    DeterministicRiskEngine,
    RiskConfig,
    RiskEvaluationRequest,
)
from tradingagents.runtime.fo_scanner import ScanCandidate, ScanEvaluationResult, SetupState
from tradingagents.runtime.market_snapshot import IST, Instrument, SessionCalendar
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService, is_transient_market_error


def _sample_option_contract(
    symbol="RELIANCE26SEP2500CE",
    underlying="RELIANCE",
    strike=2500.0,
    opt_type=OptionType.CE,
    lot_size=250,
):
    return ContractSpec(
        underlying=underlying,
        exchange=Exchange.NFO,
        trading_symbol=symbol,
        symbol_token="999001",
        instrument_type=DerivativeType.OPTSTK,
        option_type=opt_type,
        strike_price=strike,
        expiry_date="2026-09-26",
        lot_size=lot_size,
    )


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
            "RELIANCE": {
                "underlying": "RELIANCE",
                "exchange": "NFO",
                "is_index": False,
                "lot_size": 250,
                "expiries": ["26SEP2026"],
            },
            "NIFTY": {
                "underlying": "NIFTY",
                "exchange": "NFO",
                "is_index": True,
                "lot_size": 25,
                "expiries": ["26SEP2026"],
            },
        }

    def get_fo_universe(self, exchange: str = "NFO"):
        return dict(self._universe)

    def discover_universe(self):
        return ["NIFTY", "RELIANCE"]


def _make_service(tmp_path, vix=14.0, max_errors=3, clock_time=None, source="demo", **kwargs):
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
        'chart': _generate_valid_candles(base_price=25000.0, n_bars=30, base_time=current_time - timedelta(minutes=150)),
    }
    feed.snapshot.return_value = snapshot
    feed.fetch.return_value = snapshot

    service = PaperTradingService(
        feed=feed,
        ledger=ledger,
        scrip_master=MockScripMaster(),
        scanner_batch_size=5,
        max_errors=max_errors,
        clock=lambda: current_time,
        calendar=SessionCalendar({'year': 2026}),
        **kwargs,
    )
    service.start(background=False)
    return service, feed, ledger, current_time


# =============================================================================
# 1. TRANSIENT FEED FAILURE RESILIENCE
# =============================================================================

@pytest.mark.parametrize("transient_reason", [
    "candle_gap",
    "insufficient_completed_candles",
    "angel_rate_limited",
    "stale_market_data",
    "stale_or_future_market_data",
    "quote_fetch_incomplete",
    "angel_timeout",
    "angel_connection_error",
])
def test_transient_market_error_classification(transient_reason):
    assert is_transient_market_error(transient_reason) is True


def test_non_transient_fatal_error_classification():
    assert is_transient_market_error("snapshot_identity_mismatch") is False
    assert is_transient_market_error("invalid_syntax_key_error") is False
    assert is_transient_market_error("database_corrupted") is False


def test_transient_errors_keep_runner_alive_beyond_max_errors(tmp_path):
    """Transient errors (e.g. candle_gap) must back off but NOT halt the runner after max_errors."""
    service, feed, _, _ = _make_service(tmp_path, max_errors=3)

    feed.fetch.side_effect = ValueError("candle_gap")

    # Simulate 5 consecutive transient failures
    for i in range(1, 6):
        res = service.tick()
        assert res['status'] == 'error'
        assert res['reason'] == 'candle_gap'
        assert service.errors == i
        assert service.running is True
        assert not service.stop_event.is_set()

    # Verify positions are marked as unknown data during data outage
    row = service.fo_orchestrator.paper_engine.conn.execute(
        "SELECT COUNT(*) FROM paper_positions WHERE is_unknown_data=1"
    ).fetchone()[0]
    # No crash and fail-closed: allow_trade is False
    assert service.last_risk['allow_trade'] is False


def test_fatal_non_transient_errors_stop_runner_at_max_errors(tmp_path):
    """Fatal errors (e.g. snapshot_identity_mismatch) MUST halt runner at max_errors."""
    service, feed, _, _ = _make_service(tmp_path, max_errors=3)

    feed.fetch.side_effect = ValueError("snapshot_identity_mismatch")

    # 1st error
    res1 = service.tick()
    assert res1['status'] == 'error'
    assert service.running is True

    # 2nd error
    res2 = service.tick()
    assert res2['status'] == 'error'
    assert service.running is True

    # 3rd error: hits max_errors -> runner stops
    res3 = service.tick()
    assert res3['status'] == 'error'
    assert service.running is False
    assert service.stop_event.is_set()


def test_successful_tick_resets_error_counters(tmp_path):
    """A successful tick clears errors and non_transient_errors."""
    service, feed, _, current_time = _make_service(tmp_path)

    # First trigger 2 transient errors
    feed.fetch.side_effect = ValueError("candle_gap")
    service.tick()
    service.tick()
    assert service.errors == 2

    # Restore valid snapshot
    snapshot = {
        'source': 'demo',
        'instrument': service.instrument.as_dict(),
        'price': 25000.0,
        'vix': 14.0,
        'atr': 120.0,
        'atr_ratio': 1.0,
        'trend_strength': 0.8,
        'timestamp': current_time.isoformat(),
        'vix_timestamp': current_time.isoformat(),
        'bar_timestamp': (current_time - timedelta(minutes=1)).isoformat(),
        'chart': _generate_valid_candles(25000.0, 30, base_time=current_time - timedelta(minutes=150)),
    }
    feed.fetch.side_effect = None
    feed.fetch.return_value = snapshot

    result = service.tick()
    assert result['status'] in ('monitoring', 'risk_blocked', 'executed_intraday_fo_paper')
    assert service.errors == 0
    assert service.non_transient_errors == 0
    assert service.last_error is None


# =============================================================================
# 2. OPTION SL / TARGET UNIT NORMALIZATION
# =============================================================================

def test_option_sl_target_derived_from_underlying_spot_units(tmp_path):
    """Spot-level SL and Target (e.g. 2480, 2540 for spot 2500) are normalized to option units."""
    engine = FOPaperTradingEngine(db_path=tmp_path / "paper.sqlite3", initial_capital=500000.0)
    risk = DeterministicRiskEngine(RiskConfig(max_loss_per_trade=5000.0))
    orchestrator = FOPipelineOrchestrator(scrip_master=MockScripMaster(), paper_engine=engine, risk_engine=risk)

    contract = _sample_option_contract()
    orchestrator.resolver.resolve = MagicMock(return_value=MagicMock(success=True, contract=contract))

    eval_time = datetime(2026, 9, 17, 10, 30, 0)
    mock_tick = MagicMock(ltp=45.0, timestamp=eval_time, bid=44.8, ask=45.2, is_stale=False)
    orchestrator.market_data_provider = MagicMock()
    orchestrator.market_data_provider.get_latest_tick.return_value = mock_tick

    result = orchestrator.process_signal(
        underlying="RELIANCE",
        spot_price=2500.0,
        bias=MarketBias.BULLISH,
        proposed_lots=1,
        stop_loss=2480.0,   # Spot SL
        target=2540.0,      # Spot Target
        evaluation_time=eval_time,
        strategy_name="TEST_SPOT_DERIVATION",
        trade_type="INTRADAY",
        data_source="SIMULATION",
    )

    assert result.success is True
    assert result.order is not None
    order = result.order
    assert order.fill_price > 0
    assert order.stop_loss is not None
    assert order.target is not None
    # Invariants: 0 < SL < entry < Target
    assert 0 < order.stop_loss < order.fill_price
    assert order.target > order.fill_price

    positions = engine.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.stop_loss == order.stop_loss
    assert pos.target == order.target


def test_invalid_option_sl_target_fails_closed(tmp_path):
    """Invalid SL (>= entry) or invalid Target (<= entry) must fail closed."""
    engine = FOPaperTradingEngine(db_path=tmp_path / "paper.sqlite3", initial_capital=500000.0)
    risk = DeterministicRiskEngine(RiskConfig(enforce_stop_loss=False))
    orchestrator = FOPipelineOrchestrator(scrip_master=MockScripMaster(), paper_engine=engine, risk_engine=risk)

    contract = _sample_option_contract()
    orchestrator.resolver.resolve = MagicMock(return_value=MagicMock(success=True, contract=contract))

    # Mock entry tick at 45.0
    mock_tick = MagicMock(ltp=45.0, timestamp=datetime.now(), bid=44.0, ask=46.0, is_stale=False)
    orchestrator.market_data_provider = MagicMock()
    orchestrator.market_data_provider.get_latest_tick.return_value = mock_tick
    eval_time = datetime(2026, 9, 17, 10, 30, 0)

    # 1. Invalid SL (e.g. negative or None when not defaultable)
    res_sl = orchestrator.process_signal(
        underlying="RELIANCE",
        spot_price=2500.0,
        bias=MarketBias.BULLISH,
        proposed_lots=1,
        stop_loss=-10.0,  # Negative SL
        target=2540.0,
        evaluation_time=eval_time,
        strategy_name="TEST_FAIL_CLOSED",
        trade_type="INTRADAY",
    )
    assert res_sl.success is False
    assert res_sl.status in ("INVALID_STOP_LOSS", "RISK_REJECTED")

    # 2. Invalid Target <= entry (e.g. 30.0 <= 45.0)
    res_tgt = orchestrator.process_signal(
        underlying="RELIANCE",
        spot_price=2500.0,
        bias=MarketBias.BULLISH,
        proposed_lots=1,
        stop_loss=2480.0,
        target=30.0,  # Option Target 30 <= 45 entry
        evaluation_time=eval_time,
        strategy_name="TEST_FAIL_CLOSED",
        trade_type="INTRADAY",
    )
    assert res_tgt.success is False
    assert res_tgt.status in ("INVALID_TARGET", "RISK_REJECTED")


# =============================================================================
# 3. POSITION SL / TARGET PERSISTENCE AND AUTOMATED EXIT TRIGGERS
# =============================================================================

def test_paper_engine_persists_sl_target_and_triggers_exits(tmp_path):
    """FOPaperTradingEngine persists SL/Target in paper_positions and triggers automated exits on mark price."""
    engine = FOPaperTradingEngine(db_path=tmp_path / "paper.sqlite3", initial_capital=500000.0)
    contract = _sample_option_contract()

    # Submit BUY order: entry 45.0, SL 35.0, Target 65.0
    order = engine.submit_order(
        contract=contract,
        action="BUY",
        lots=1,
        current_price=45.0,
        stop_loss=35.0,
        target=65.0,
        idempotency_key="TEST_PERSIST_001",
        data_source="SIMULATION",
    )
    assert order.status == "FILLED"

    # Verify positions table has stop_loss and target stored
    positions = engine.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.stop_loss == 35.0
    assert pos.target == 65.0
    assert pos.current_mark > 0

    # Test Target Hit: mark price rises to 66.0 >= 65.0 Target
    res_mark = engine.update_mark_price(pos.symbol, 66.0)
    assert res_mark["triggered_exit"] == "TARGET_HIT"
    assert len(engine.get_open_positions()) == 0

    # Submit second order for SL Hit testing
    order2 = engine.submit_order(
        contract=contract,
        action="BUY",
        lots=1,
        current_price=45.0,
        stop_loss=35.0,
        target=65.0,
        idempotency_key="TEST_PERSIST_002",
        data_source="SIMULATION",
    )
    assert order2.status == "FILLED"

    # Test Stop Loss Hit: mark price drops to 34.0 <= 35.0 SL
    res_sl = engine.update_mark_price(pos.symbol, 34.0)
    assert res_sl["triggered_exit"] == "STOP_LOSS_HIT"
    assert len(engine.get_open_positions()) == 0


def test_sync_fo_positions_updates_marks_with_option_quotes_not_equity_price(tmp_path):
    """_sync_fo_positions() updates option positions using option quotes, not equity snapshot price."""
    service, _, _, _ = _make_service(tmp_path)
    contract = _sample_option_contract()

    service.fo_orchestrator.paper_engine.submit_order(
        contract=contract,
        action="BUY",
        lots=1,
        current_price=45.0,
        stop_loss=35.0,
        target=65.0,
        idempotency_key="TEST_SYNC_001",
        data_source="SIMULATION",
    )

    # Mock market adapter get_quote returning option quote (50.0)
    mock_adapter = MagicMock()
    mock_adapter.get_quote.return_value = {'symbol': contract.trading_symbol, 'ltp': 50.0}

    # Snapshot price is 25000.0 (Index equity level)
    snapshot = {'price': 25000.0, 'timestamp': datetime.now(IST).isoformat()}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._sync_fo_positions(snapshot)

    pos = service.fo_orchestrator.paper_engine.get_open_positions()[0]
    # Mark price must be updated to option quote 50.0, NEVER equity price 25000.0!
    assert pos.current_mark == 50.0
    assert pos.current_mark != 25000.0


def test_option_sl_target_not_triggered_on_synthetic_data_when_quotes_unavailable(tmp_path):
    """When real option quotes are unavailable or stale, do NOT use synthetic estimate to trigger SL/Target."""
    service, _, _, _ = _make_service(tmp_path)
    contract = _sample_option_contract()

    service.fo_orchestrator.paper_engine.submit_order(
        contract=contract,
        action="BUY",
        lots=1,
        current_price=45.0,
        stop_loss=35.0,
        target=65.0,
        idempotency_key="TEST_SAFETY_001",
        data_source="SIMULATION",
    )

    # 1. Simulate both live tick and quote API unavailable/stale
    mock_adapter = MagicMock()
    mock_adapter.get_quote.return_value = None  # No option quote available
    snapshot = {'price': 24000.0, 'timestamp': datetime.now(IST).isoformat()}  # Deeply dropped underlying

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._sync_fo_positions(snapshot)

    # Position must NOT be exited synthetically! Must remain open with is_unknown_data=True
    positions = service.fo_orchestrator.paper_engine.get_open_positions()
    assert len(positions) == 1
    assert positions[0].is_unknown_data is True

    # 2. When real option quote arrives showing SL breached (e.g. 30.0 <= 35.0)
    mock_adapter.get_quote.return_value = {'symbol': contract.trading_symbol, 'ltp': 30.0, 'is_stale': False}
    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._sync_fo_positions(snapshot)

    # Position should now exit on REAL option quote data
    positions_after = service.fo_orchestrator.paper_engine.get_open_positions()
    assert len(positions_after) == 0

