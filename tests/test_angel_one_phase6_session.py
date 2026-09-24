"""
Unit tests for Angel One Integration Phase 6: Real-Time Paper Trading Session & Monitoring.
Verifies:
- 10-point startup checklist & safe halt on failures
- Crash & restart recovery with state reconciliation
- Market hours / calendar detection (Weekend / Pre-market / Post-market)
- Observability single-line status formatting
- Daily session report generation with live vs sim isolation
- No forced trading behavior (0 trades is valid)
- Absolute safety: Broker write APIs remain unreachable.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from tradingagents.integrations.angel_one.client import AngelOneClient
from tradingagents.integrations.angel_one.market_data import (
    AngelOneMarketDataProvider,
    ConnectionState,
    DataSource,
)
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
from tradingagents.integrations.angel_one.session_manager import (
    MarketSessionObservability,
    PaperSessionConfig,
    SessionRecoveryManager,
    SessionStartupChecker,
    SessionStatus,
)


@pytest.fixture
def mock_scrip_manager(tmp_path):
    """Set up ScripMasterManager with mock data for all 6 target indices."""
    today = date.today()
    future_expiry = (today + timedelta(days=7)).strftime("%d%b%Y").upper()

    instruments = [
        {"token": "1001", "symbol": f"NIFTY{future_expiry}25000CE", "name": "NIFTY", "expiry": future_expiry, "strike": "2500000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "2001", "symbol": f"BANKNIFTY{future_expiry}52000CE", "name": "BANKNIFTY", "expiry": future_expiry, "strike": "5200000", "lotsize": "15", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "3001", "symbol": f"SENSEX{future_expiry}80000CE", "name": "SENSEX", "expiry": future_expiry, "strike": "8000000", "lotsize": "10", "instrumenttype": "OPTIDX", "exch_seg": "BFO"},
        {"token": "4001", "symbol": f"FINNIFTY{future_expiry}23000CE", "name": "FINNIFTY", "expiry": future_expiry, "strike": "2300000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "5001", "symbol": f"MIDCPNIFTY{future_expiry}12500CE", "name": "MIDCPNIFTY", "expiry": future_expiry, "strike": "1250000", "lotsize": "50", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "6001", "symbol": f"BANKEX{future_expiry}58000CE", "name": "BANKEX", "expiry": future_expiry, "strike": "5800000", "lotsize": "15", "instrumenttype": "OPTIDX", "exch_seg": "BFO"},
    ]

    cache_file = tmp_path / "scrip_master.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(instruments, f)

    mgr = ScripMasterManager(cache_dir=tmp_path)
    mgr.load(force_download=False)
    return mgr


@pytest.fixture
def mock_client():
    """Mock AngelOneClient with read-only credentials."""
    client = AngelOneClient(api_key="TEST", client_code="TEST", pin="1234", totp_secret="TEST")
    client._is_authenticated = True
    return client


# =============================================================================
# 1. CONFIGURABLE PARAMETERS TESTS
# =============================================================================
def test_session_config_validation():
    """Verify safe bounds and validation for session parameters."""
    # Valid config
    cfg = PaperSessionConfig(
        confidence_threshold=0.65,
        signal_stale_age_seconds=250.0,
        tick_stale_threshold_seconds=4.0,
        default_slippage_pct=0.75,
    )
    assert cfg.confidence_threshold == 0.65
    assert cfg.signal_stale_age_seconds == 250.0

    # Invalid confidence (< 0.50) must raise ValidationError
    with pytest.raises(Exception):
        PaperSessionConfig(confidence_threshold=0.30)


# =============================================================================
# 2. STARTUP CHECKLIST TESTS
# =============================================================================
def test_startup_checklist_success(mock_scrip_manager, mock_client):
    """When all systems are healthy, checklist passes and transitions to WAITING_FOR_MARKET."""
    cfg = PaperSessionConfig()
    paper_engine = FOPaperTradingEngine(db_path=":memory:")
    risk_engine = DeterministicRiskEngine()
    data_provider = AngelOneMarketDataProvider(client=mock_client)
    data_provider.state = ConnectionState.CONNECTED

    checker = SessionStartupChecker(
        config=cfg,
        client=mock_client,
        scrip_master=mock_scrip_manager,
        paper_engine=paper_engine,
        risk_engine=risk_engine,
        market_data_provider=data_provider,
    )

    result = checker.run_pre_session_checklist()
    assert result.passed is True
    assert result.status == SessionStatus.WAITING_FOR_MARKET
    assert len(result.failures) == 0
    assert result.checklist["9_broker_write_apis_blocked"] is True


def test_startup_checklist_safe_halt_on_active_kill_switch(mock_scrip_manager, mock_client):
    """If kill switch is active on startup, checklist must trigger SAFE_HALT."""
    cfg = PaperSessionConfig()
    paper_engine = FOPaperTradingEngine(db_path=":memory:")
    risk_engine = DeterministicRiskEngine()
    risk_engine.trigger_kill_switch("Active previous risk event")

    checker = SessionStartupChecker(
        config=cfg,
        client=mock_client,
        scrip_master=mock_scrip_manager,
        paper_engine=paper_engine,
        risk_engine=risk_engine,
    )

    result = checker.run_pre_session_checklist()
    assert result.passed is False
    assert result.status == SessionStatus.SAFE_HALT
    assert any("kill-switch" in f for f in result.failures)


# =============================================================================
# 3. CRASH & RESTART RECOVERY TESTS
# =============================================================================
def test_crash_recovery_reconciliation(mock_scrip_manager, mock_client, tmp_path):
    """Verify recovery when restarting with open positions in SQLite ledger."""
    db_file = tmp_path / "paper_ledger.db"

    # 1. Setup session with 1 open position
    paper_engine1 = FOPaperTradingEngine(db_path=db_file)
    from tradingagents.integrations.angel_one.models import ContractSpec, DerivativeType, Exchange, OptionType
    contract = ContractSpec(
        underlying="NIFTY",
        exchange=Exchange.NFO,
        trading_symbol="NIFTY24OCT25000CE",
        symbol_token="1001",
        instrument_type=DerivativeType.OPTIDX,
        option_type=OptionType.CE,
        strike_price=25000.0,
        expiry_date="24OCT2024",
        lot_size=65,
    )
    paper_engine1.submit_order(contract=contract, action="BUY", lots=1, current_price=180.0)
    assert len(paper_engine1.get_open_positions()) == 1

    # 2. Simulate restart: Reopen from same DB with feed disconnected
    paper_engine2 = FOPaperTradingEngine(db_path=db_file)
    data_provider = AngelOneMarketDataProvider(client=mock_client)
    data_provider.state = ConnectionState.DISCONNECTED  # Feed not yet live

    rec_result = SessionRecoveryManager.reconcile_on_startup(
        paper_engine=paper_engine2,
        market_data_provider=data_provider,
    )

    assert rec_result["status"] == "RECONCILED"
    assert rec_result["recovered_positions_count"] == 1
    assert rec_result["data_paused"] is True
    # Position must be marked is_unknown_data=True to pause blind exits
    open_pos = paper_engine2.get_open_positions()
    assert open_pos[0].is_unknown_data is True


# =============================================================================
# 4. MARKET CALENDAR & OBSERVABILITY TESTS
# =============================================================================
def test_market_calendar_detection():
    """Verify Indian market open/close detection across time scenarios."""
    # Weekend (Sunday)
    sunday_dt = datetime(2024, 10, 27, 11, 0, 0)
    is_open_sun, reason_sun = MarketSessionObservability.is_market_open(sunday_dt)
    assert is_open_sun is False
    assert "Weekend" in reason_sun

    # Weekday 10:30 AM (In session)
    wed_open_dt = datetime(2024, 10, 23, 10, 30, 0)
    is_open_wed, _ = MarketSessionObservability.is_market_open(wed_open_dt)
    assert is_open_wed is True

    # Weekday 09:00 AM (Pre-market)
    wed_early_dt = datetime(2024, 10, 23, 9, 0, 0)
    is_open_early, reason_early = MarketSessionObservability.is_market_open(wed_early_dt)
    assert is_open_early is False
    assert "Pre-market" in reason_early


def test_observability_status_line(mock_scrip_manager, mock_client):
    """Verify single-line status formatting."""
    orchestrator = FOPipelineOrchestrator(
        scrip_master=mock_scrip_manager,
        paper_engine=FOPaperTradingEngine(db_path=":memory:"),
        risk_engine=DeterministicRiskEngine(),
    )

    status_line = MarketSessionObservability.get_status_line(
        orchestrator=orchestrator,
        check_time=datetime(2024, 10, 27, 12, 0, 0),  # Weekend
    )
    assert "SYSTEM: RUNNING" in status_line
    assert "MODE: PAPER" in status_line
    assert "MARKET: CLOSED" in status_line
    assert "TRADING: WAITING_FOR_MARKET" in status_line
    assert "OPEN PAPER POSITIONS: 0" in status_line


# =============================================================================
# 5. DAILY REPORT GENERATION & NO FORCED TRADES
# =============================================================================
def test_daily_session_report_with_zero_trades(mock_scrip_manager):
    """When 0 trades occur (no forced trades), report cleanly reflects 0 trades."""
    orchestrator = FOPipelineOrchestrator(
        scrip_master=mock_scrip_manager,
        paper_engine=FOPaperTradingEngine(db_path=":memory:"),
        risk_engine=DeterministicRiskEngine(),
    )

    start_time = datetime.now() - timedelta(hours=6)
    report = MarketSessionObservability.generate_daily_session_report(
        orchestrator=orchestrator,
        session_start_time=start_time,
    )

    assert "# 📋 Indian F&O Real-Time Paper Trading Daily Report" in report
    assert "Paper Trades Closed: `0`" in report
    assert "Net Realized P&L: `₹0.00`" in report
    assert "REAL-TIME PAPER TRADING — NO REAL MONEY ORDERS" in report
    assert "Real-Time Paper vs Simulation Isolation" in report


# =============================================================================
# 6. ABSOLUTE SAFETY: BROKER WRITE APIS UNREACHABLE
# =============================================================================
def test_broker_write_apis_unreachable(mock_client):
    """Verify that placeOrder, modifyOrder, cancelOrder cannot be called."""
    with pytest.raises(NotImplementedError):
        mock_client.place_order()

    with pytest.raises(NotImplementedError):
        mock_client.modify_order()

    with pytest.raises(NotImplementedError):
        mock_client.cancel_order()
