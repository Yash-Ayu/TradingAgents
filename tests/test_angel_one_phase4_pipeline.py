"""
Unit tests for Angel One Integration Phase 4: End-to-End Pipeline Orchestrator & Monitoring.
Verifies full pipeline flow from AI signal -> Contract resolution -> Risk gate -> Paper execution,
tick monitoring, automated exit alerts, emergency stop, and validation reporting.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tradingagents.integrations.angel_one.models import MarketBias, StrikeMode
from tradingagents.integrations.angel_one.orchestrator import (
    FOPipelineOrchestrator,
    PhoneAlertDispatcher,
)
from tradingagents.integrations.angel_one.paper_engine import FOPaperTradingEngine
from tradingagents.integrations.angel_one.risk_engine import (
    DeterministicRiskEngine,
    RiskConfig,
)
from tradingagents.integrations.angel_one.scrip_master import ScripMasterManager


@pytest.fixture
def mock_scrip_manager(tmp_path):
    """Set up ScripMasterManager with mock data."""
    today = date.today()
    future_expiry = (today + timedelta(days=7)).strftime("%d%b%Y").upper()

    instruments = [
        {"token": "1001", "symbol": f"NIFTY{future_expiry}24500CE", "name": "NIFTY", "expiry": future_expiry, "strike": "2450000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "1002", "symbol": f"NIFTY{future_expiry}24500PE", "name": "NIFTY", "expiry": future_expiry, "strike": "2450000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "2001", "symbol": f"BANKNIFTY{future_expiry}52000CE", "name": "BANKNIFTY", "expiry": future_expiry, "strike": "5200000", "lotsize": "15", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
    ]

    cache_file = tmp_path / "scrip_master.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(instruments, f)

    mgr = ScripMasterManager(cache_dir=tmp_path)
    mgr.load(force_download=False)
    return mgr


@pytest.fixture
def orchestrator(mock_scrip_manager):
    """Set up FOPipelineOrchestrator with in-memory paper ledger and mock scrip data."""
    risk_config = RiskConfig(
        max_capital_per_trade=50000.0,
        max_lots_per_trade=2,
        max_open_positions=2,
        enforce_market_hours=False,  # Allow test execution at any time
        enforce_stop_loss=True,
    )
    risk_engine = DeterministicRiskEngine(config=risk_config)
    paper_engine = FOPaperTradingEngine(db_path=":memory:", initial_capital=100000.0)
    alert_dispatcher = PhoneAlertDispatcher()  # Local logger mode

    return FOPipelineOrchestrator(
        scrip_master=mock_scrip_manager,
        risk_engine=risk_engine,
        paper_engine=paper_engine,
        alert_dispatcher=alert_dispatcher,
    )


# =============================================================================
# 1. END-TO-END PIPELINE TESTS
# =============================================================================
def test_pipeline_successful_execution(orchestrator):
    """Valid signal should successfully traverse resolution, risk, and paper execution."""
    result = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=24505.0,  # ATM 24500
        bias=MarketBias.BULLISH,
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=160.0,
        target=250.0,
    )

    assert result.success is True
    assert result.status == "EXECUTED_PAPER"
    assert result.contract is not None
    assert result.contract.strike_price == 24500.0
    assert result.contract.option_type.value == "CE"
    assert result.order is not None
    assert result.order.status == "FILLED"

    # Verify position is recorded in both Paper Engine and Risk Engine
    open_pos = orchestrator.paper_engine.get_open_positions()
    assert len(open_pos) == 1
    assert open_pos[0].symbol == result.contract.trading_symbol
    assert result.contract.trading_symbol in orchestrator.risk_engine.current_open_positions


def test_pipeline_contract_resolution_failure(orchestrator):
    """Unknown symbol should halt the pipeline at contract resolution."""
    result = orchestrator.process_signal(
        underlying="NON_EXISTENT_INDEX",
        spot_price=1000.0,
        bias=MarketBias.BULLISH,
    )

    assert result.success is False
    assert result.status == "CONTRACT_RESOLUTION_FAILED"
    assert "Unknown instrument" in result.reason
    assert result.order is None


def test_pipeline_risk_rejection(orchestrator):
    """Trade violating risk rules (e.g. excessive lots) should halt at Risk Engine."""
    result = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=24500.0,
        bias=MarketBias.BULLISH,
        proposed_lots=5,  # Exceeds max 2 lots
        entry_price=200.0,
        stop_loss=160.0,
    )

    assert result.success is False
    assert result.status == "RISK_REJECTED"
    assert result.risk_evaluation.violation_code == "EXCEEDS_MAX_LOTS"
    assert result.order is None
    assert len(orchestrator.paper_engine.get_open_positions()) == 0


# =============================================================================
# 2. TICK MONITORING & AUTO EXIT TESTS
# =============================================================================
def test_tick_monitoring_and_auto_exit(orchestrator):
    """Market tick updating below SL should trigger automated exit and update risk engine."""
    res = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=24500.0,
        bias=MarketBias.BULLISH,
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=170.0,
        target=250.0,
    )
    symbol = res.contract.trading_symbol

    # 1. Normal tick above SL
    tick1 = orchestrator.on_market_tick(symbol=symbol, ltp=190.0)
    assert tick1["triggered_exit"] is None
    assert len(orchestrator.paper_engine.get_open_positions()) == 1

    # 2. Tick below SL (165 < 170)
    tick2 = orchestrator.on_market_tick(symbol=symbol, ltp=165.0)
    assert tick2["triggered_exit"] == "STOP_LOSS_HIT"
    assert len(orchestrator.paper_engine.get_open_positions()) == 0


# =============================================================================
# 3. EMERGENCY KILL SWITCH & VALIDATION REPORT
# =============================================================================
def test_emergency_stop_all(orchestrator):
    """Emergency stop should close all open positions and lock the engine."""
    # Open a position
    res = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=24500.0,
        bias=MarketBias.BULLISH,
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
    )
    assert len(orchestrator.paper_engine.get_open_positions()) == 1

    # Trigger emergency stop
    stop_res = orchestrator.emergency_stop_all(reason="Owner clicked Emergency Kill Switch")
    assert stop_res["status"] == "EMERGENCY_LOCKED"
    assert stop_res["closed_positions_count"] == 1
    assert len(orchestrator.paper_engine.get_open_positions()) == 0
    assert orchestrator.risk_engine.is_emergency_locked is True

    # Subsequent signal must be rejected
    res_after = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=24500.0,
        bias=MarketBias.BULLISH,
        entry_price=200.0,
    )
    assert res_after.success is False
    assert res_after.status == "RISK_REJECTED"
    assert res_after.risk_evaluation.violation_code == "KILL_SWITCH_ACTIVE"


def test_validation_report_generation(orchestrator):
    """Validation report should generate complete markdown scorecard."""
    report = orchestrator.generate_validation_report()
    assert "# 📈 Indian F&O Paper Trading Validation Report" in report
    assert "Portfolio & Capital Summary" in report
    assert "Performance Metrics & Scorecard" in report
    assert "**Initial Capital:** ₹100,000.00" in report
