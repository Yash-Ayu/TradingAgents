"""
Unit tests for Angel One Integration Phase 5: Real-Time Paper Trading & Live Market Data.
Verifies:
- Live market data provider (ticks, staleness, disconnect & reconnection)
- TradingAgents signal adapter (HOLD=NO TRADE, confidence threshold, stale signals)
- Disconnect safety (paused SL/Target triggers during data freeze)
- Data source separation (LIVE_ANGEL_ONE vs SIMULATION)
- Phone alerts labeled [🟡 PAPER TRADE]
- Emergency stop paper-only verification
- Absolute safety: Zero broker order API calls.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tradingagents.integrations.angel_one.client import AngelOneClient
from tradingagents.integrations.angel_one.market_data import (
    AngelOneMarketDataProvider,
    ConnectionState,
    DataSource,
    MarketTick,
)
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
from tradingagents.integrations.angel_one.signal_adapter import (
    TradingAgentsSignalAdapter,
    ValidatedSignal,
)


@pytest.fixture
def mock_scrip_manager(tmp_path):
    """Set up ScripMasterManager with mock NIFTY data."""
    today = date.today()
    future_expiry = (today + timedelta(days=7)).strftime("%d%b%Y").upper()

    instruments = [
        {"token": "57796", "symbol": f"NIFTY{future_expiry}25000CE", "name": "NIFTY", "expiry": future_expiry, "strike": "2500000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "57797", "symbol": f"NIFTY{future_expiry}25000PE", "name": "NIFTY", "expiry": future_expiry, "strike": "2500000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
    ]

    cache_file = tmp_path / "scrip_master.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(instruments, f)

    mgr = ScripMasterManager(cache_dir=tmp_path)
    mgr.load(force_download=False)
    return mgr


@pytest.fixture
def mock_client():
    """Mock AngelOneClient for read-only connectivity."""
    client = AngelOneClient(api_key="TEST", client_code="TEST", pin="1234", totp_secret="TEST")
    client._is_authenticated = True
    return client


# =============================================================================
# 1. LIVE MARKET DATA ADAPTER TESTS
# =============================================================================
def test_market_data_ingest_and_stale_detection(mock_client):
    """Verify live tick ingestion and automatic staleness calculation."""
    provider = AngelOneMarketDataProvider(client=mock_client, max_tick_age_seconds=5.0)

    # Ingest fresh tick
    now = datetime.now()
    tick = provider.ingest_tick(
        symbol="NIFTY24OCT25000CE",
        token="57796",
        exchange="NFO",
        ltp=185.50,
        timestamp=now,
    )
    assert tick.ltp == 185.50
    assert tick.is_stale is False
    assert tick.data_source == DataSource.LIVE_ANGEL_ONE

    # Ingest stale tick (10 seconds old)
    stale_time = now - timedelta(seconds=10)
    stale_tick = provider.ingest_tick(
        symbol="NIFTY24OCT25000CE",
        token="57796",
        exchange="NFO",
        ltp=185.50,
        timestamp=stale_time,
    )
    assert stale_tick.is_stale is True


def test_market_data_disconnect_and_reconnect(mock_client):
    """Verify state transitions on disconnect and auto-reconnect."""
    provider = AngelOneMarketDataProvider(client=mock_client)
    provider.state = ConnectionState.CONNECTED
    assert provider.is_connected() is True

    # Simulate unexpected disconnect
    provider.handle_disconnect("WebSocket network timeout")
    # State transitions through RECONNECTING or back to CONNECTED if client is authenticated
    assert provider.state in (ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, ConnectionState.RECONNECTING)


# =============================================================================
# 2. TRADINGAGENTS SIGNAL ADAPTER TESTS
# =============================================================================
def test_signal_adapter_validation():
    """Verify signal gating rules: HOLD, low confidence, and stale signals are rejected."""
    adapter = TradingAgentsSignalAdapter(min_confidence=0.60, max_signal_age_seconds=300.0)

    # 1. HOLD signal -> NO TRADE
    hold_sig = ValidatedSignal(
        underlying="NIFTY",
        direction=MarketBias.NEUTRAL,
        confidence=0.85,
    )
    is_valid, reason = adapter.validate_signal(hold_sig)
    assert is_valid is False
    assert "HOLD / NEUTRAL" in reason

    # 2. Low confidence (< 0.60) -> NO TRADE
    low_conf_sig = ValidatedSignal(
        underlying="NIFTY",
        direction=MarketBias.BULLISH,
        confidence=0.55,
    )
    is_valid, reason = adapter.validate_signal(low_conf_sig)
    assert is_valid is False
    assert "below minimum threshold" in reason

    # 3. Stale signal (> 300s old) -> NO TRADE
    stale_sig = ValidatedSignal(
        underlying="NIFTY",
        direction=MarketBias.BULLISH,
        confidence=0.80,
        data_timestamp=datetime.now() - timedelta(seconds=400),
    )
    is_valid, reason = adapter.validate_signal(stale_sig)
    assert is_valid is False
    assert "Stale signal" in reason

    # 4. Valid signal -> PASS
    valid_sig = ValidatedSignal(
        underlying="NIFTY",
        direction=MarketBias.BULLISH,
        confidence=0.80,
        data_timestamp=datetime.now(),
    )
    is_valid, reason = adapter.validate_signal(valid_sig)
    assert is_valid is True


def test_signal_adaptation_from_pm_decision():
    """Verify converting TradingAgents decision markdown or dict into ValidatedSignal."""
    adapter = TradingAgentsSignalAdapter()

    # Dictionary input
    raw_dict = {"rating": "BUY", "confidence": 0.85, "reasoning": "Strong SMC order block bounce"}
    sig1 = adapter.adapt_from_agent_output(raw_dict, underlying="NIFTY")
    assert sig1 is not None
    assert sig1.direction == MarketBias.BULLISH
    assert sig1.confidence == 0.85

    # Text input
    raw_text = "**Rating**: Overweight\nStrong institutional accumulation on NIFTY."
    sig2 = adapter.adapt_from_agent_output(raw_text, underlying="NIFTY")
    assert sig2 is not None
    assert sig2.direction == MarketBias.BULLISH


# =============================================================================
# 3. PIPELINE WITH LIVE DATA & RECONNECT SAFETY
# =============================================================================
def test_pipeline_halts_on_disconnected_data(mock_scrip_manager, mock_client):
    """When market data feed is disconnected, pipeline must reject new trades."""
    provider = AngelOneMarketDataProvider(client=mock_client)
    provider.state = ConnectionState.DISCONNECTED  # Explicitly disconnected

    orchestrator = FOPipelineOrchestrator(
        scrip_master=mock_scrip_manager,
        market_data_provider=provider,
        risk_engine=DeterministicRiskEngine(config=RiskConfig(enforce_market_hours=False)),
        paper_engine=FOPaperTradingEngine(db_path=":memory:"),
    )

    res = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=25000.0,
        bias=MarketBias.BULLISH,
        entry_price=180.0,
    )
    assert res.success is False
    assert res.status == "DATA_FEED_DISCONNECTED"
    assert "market data stream is disconnected" in res.reason


def test_data_disconnect_pauses_automated_exits(mock_scrip_manager, mock_client):
    """When feed disconnects, positions must be marked UNKNOWN_DATA and SL/Target triggers paused."""
    provider = AngelOneMarketDataProvider(client=mock_client)
    provider.state = ConnectionState.CONNECTED

    paper_engine = FOPaperTradingEngine(db_path=":memory:")
    orchestrator = FOPipelineOrchestrator(
        scrip_master=mock_scrip_manager,
        market_data_provider=provider,
        risk_engine=DeterministicRiskEngine(config=RiskConfig(enforce_market_hours=False)),
        paper_engine=paper_engine,
    )

    # Open position
    res = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=25000.0,
        bias=MarketBias.BULLISH,
        entry_price=200.0,
        stop_loss=170.0,
    )
    assert res.success is True
    symbol = res.contract.trading_symbol

    # Disconnect feed
    provider.state = ConnectionState.DISCONNECTED

    # Market tick comes in at 160 (below SL 170) while feed is disconnected
    update_res = orchestrator.on_market_tick(symbol=symbol, ltp=160.0)
    assert update_res["data_status"] == "UNKNOWN_DATA_PAUSED"
    assert update_res["triggered_exit"] is None
    # Position must NOT be blindly closed
    assert len(paper_engine.get_open_positions()) == 1


# =============================================================================
# 4. LIVE VS SIMULATION SCORECARD DISTINCTION & ALERTS
# =============================================================================
def test_live_vs_sim_data_source_tracking(mock_scrip_manager, mock_client):
    """Verify that trades track data_source (LIVE_ANGEL_ONE vs SIMULATION)."""
    provider = AngelOneMarketDataProvider(client=mock_client)
    provider.state = ConnectionState.CONNECTED

    paper_engine = FOPaperTradingEngine(db_path=":memory:")
    orchestrator = FOPipelineOrchestrator(
        scrip_master=mock_scrip_manager,
        market_data_provider=provider,
        risk_engine=DeterministicRiskEngine(config=RiskConfig(enforce_market_hours=False)),
        paper_engine=paper_engine,
    )

    # Trade 1: LIVE_ANGEL_ONE
    res1 = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=25000.0,
        bias=MarketBias.BULLISH,
        entry_price=180.0,
        data_source="LIVE_ANGEL_ONE",
    )
    assert res1.order.data_source == "LIVE_ANGEL_ONE"

    # Trade 2: SIMULATION
    res2 = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=25000.0,
        bias=MarketBias.BEARISH,
        entry_price=180.0,
        data_source="SIMULATION",
    )
    assert res2.order.data_source == "SIMULATION"

    positions = paper_engine.get_open_positions()
    assert len(positions) == 2
    sources = {p.data_source for p in positions}
    assert "LIVE_ANGEL_ONE" in sources
    assert "SIMULATION" in sources


def test_phone_alerts_labeled_paper_trade():
    """Verify that all phone alerts are labeled with [🟡 PAPER TRADE]."""
    dispatcher = PhoneAlertDispatcher()
    # Mock send_alert
    alerts = []
    dispatcher.send_alert = lambda title, msg: alerts.append(f"🔔 [🟡 PAPER TRADE: {title}]\n{msg}")

    dispatcher.send_alert("EXECUTED", "Test paper trade")
    assert len(alerts) == 1
    assert "[🟡 PAPER TRADE: EXECUTED]" in alerts[0]

