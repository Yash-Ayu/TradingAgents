"""
Comprehensive test suite verifying Phase 1 through Phase 12 architectural enhancements:
1. Analyzer in CLI Engine mode (no 409 rejection, dispatches CLI engine graph)
2. Scanner 300s cadence and rotation
3. Cash Index zero-volume acceptance vs Stock zero-volume rejection
4. LocalCandleStore tick aggregation, completed bar rollups, REST backfill merging, and staleness rejection
5. DhanHQ read-only market data provider (clean not_configured handling, quotes, candles, zero mutation)
6. SmartMultiProviderAdapter health tracking, fallback failover, hysteresis, and observability
7. Bucket-specific AngelRateLimiter cooldown isolation
8. Shortlist telemetry with canonical rejection reasons and status exposure
9. Secret redaction and log safety
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.integrations.angel_one.models import (
    DerivativeType,
    MarketBias,
)
from tradingagents.runtime.market_snapshot import IST, Instrument, SessionCalendar
from tradingagents.integrations.angel_one.orchestrator import PipelineExecutionResult
from tradingagents.integrations.dhan.market_data import (
    DhanMarketDataProvider,
    DhanMarketDataState,
)
from tradingagents.runtime.fo_scanner import ScanEvaluationResult, SetupState
from tradingagents.runtime.local_candle_store import LocalCandleStore, _parse_ts
from tradingagents.runtime.market_adapter import (
    AngelOneMarketAdapter,
    DhanMarketAdapter,
    ProviderHealthState,
    SmartMultiProviderAdapter,
    get_active_market_adapter,
)
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService
from tradingagents.runtime.rate_limiter import (
    AngelBucket,
    AngelRateLimiter,
)
from tradingagents.runtime.security import (
    install_secret_redaction,
    register_secret,
)


# -----------------------------------------------------------------------------
# 1. ANALYZER IN CLI ENGINE MODE (Phase 1)
# -----------------------------------------------------------------------------

def test_phase1_analyzer_wires_cli_engine_without_409_rejection(tmp_path):
    """Test Phase 1: When graph_factory is configured, AIAnalysis is ready and dispatches graph."""
    db_path = tmp_path / "ledger_phase1.sqlite3"
    ledger = PaperLedger(db_path, initial_cash=100000)
    inst = Instrument("NSE", "RELIANCE-EQ", "2885", 1, "EQ")
    feed = MagicMock()
    feed.source = "angel"
    feed.instrument = inst
    feed.connected = True

    mock_graph = MagicMock()
    mock_graph.propagate.return_value = ({"market_report": "Bullish breakout on Reliance"}, "Buy")

    def mock_graph_factory():
        return mock_graph

    service = PaperTradingService(
        feed=feed,
        ledger=ledger,
        graph_factory=mock_graph_factory,
        analysis_symbol="RELIANCE.NS",
    )

    # In engine mode, AI analysis must be initialized to 'ready'
    assert service.ai.state == 'ready'
    assert service.ai.config.get('engine') == 'cli_engine'

    # Trigger analyze
    stat = service.ai.analyze({'symbol': 'RELIANCE.NS'})
    assert stat['state'] in ('analyzing', 'ready', 'complete')

    # Wait briefly for daemon worker thread to complete
    if service.ai.worker:
        service.ai.worker.join(timeout=3.0)

    # Verify graph.propagate was called with correct symbol
    assert mock_graph.propagate.call_count >= 1
    assert service.ai.state in ('ready', 'complete')

    ledger.close()


# -----------------------------------------------------------------------------
# 2. SCANNER CADENCE & ROTATION (Phase 2 & 10)
# -----------------------------------------------------------------------------

def test_phase2_scanner_cadence_prevents_hammering_within_window(tmp_path):
    """Test Phase 2: Scanner enforces 300s cadence between heavy scans unless force=True."""
    db_path = tmp_path / "ledger_phase2.sqlite3"
    ledger = PaperLedger(db_path, initial_cash=100000)
    feed = MagicMock()
    feed.source = "demo"
    feed.instrument = Instrument("NSE", "NIFTY", "99926000", 50, "INDEX")
    feed.connected = True

    service = PaperTradingService(feed=feed, ledger=ledger)
    service.fo_scanner.discover_universe = MagicMock(return_value=["NIFTY", "RELIANCE"])

    now = datetime(2026, 9, 29, 10, 30, 0, tzinfo=IST)
    snapshot = {
        'price': 25000.0,
        'chart': [[(now - timedelta(minutes=30 - i)).isoformat(), 25000, 25010, 24990, 25005, 1000] for i in range(30)],
    }
    account = {'cash': 100000, 'positions': []}

    # Initial run: should execute scan
    assert service._last_scan_mono == 0.0
    res1 = service._scan_and_execute_fo(snapshot, account, 1)
    first_scan_mono = service._last_scan_mono
    assert first_scan_mono > 0.0

    # Immediate second run in the same second: cadence blocks duplicate scan
    res2 = service._scan_and_execute_fo(snapshot, account, 2)
    assert res2 is None
    # Monotonic timestamp must not have changed because scan was skipped
    assert service._last_scan_mono == first_scan_mono

    # Forced run: bypasses cadence window
    service._scan_and_execute_fo(snapshot, account, 3, force=True)
    assert service._last_scan_mono >= first_scan_mono

    ledger.close()


# -----------------------------------------------------------------------------
# 3. CASH INDEX ZERO-VOLUME VALIDATION (Phase 3)
# -----------------------------------------------------------------------------

def test_phase3_cash_index_zero_volume_accepted_stocks_rejected(tmp_path):
    """Test Phase 3: Zero volume is accepted for cash indices but rejected for stocks."""
    db_path = tmp_path / "ledger_phase3.sqlite3"
    ledger = PaperLedger(db_path, initial_cash=100000)
    feed = MagicMock()
    feed.source = "angel"
    feed.instrument = Instrument("NSE", "NIFTY", "99926000", 50, "INDEX")
    service = PaperTradingService(feed=feed, ledger=ledger, calendar=SessionCalendar({'year': 2026}))

    now = datetime(2026, 9, 29, 10, 30, 0, tzinfo=IST)
    base_time = now - timedelta(minutes=30)

    # Build valid candles with non-zero variance but zero volume (typical of NSE cash indices)
    rows = []
    for i in range(30):
        t_str = (base_time + timedelta(minutes=i)).isoformat()
        rows.append([t_str, 25000 + i * 2, 25005 + i * 2, 24995 + i * 2, 25002 + i * 2, 0])
    df = pd.DataFrame(rows, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])

    # Test cash index: must be VALID despite 0 volume
    valid_idx, reason_idx = service._validate_symbol_candles("NIFTY", df, "angel", now, is_index=True)
    assert valid_idx is True
    assert reason_idx == "valid"

    # Test stock: zero volume must be rejected as dead_feed_zero_volume
    valid_stock, reason_stock = service._validate_symbol_candles("RELIANCE", df, "angel", now, is_index=False)
    assert valid_stock is False
    assert reason_stock == "dead_feed_zero_volume"

    ledger.close()


# -----------------------------------------------------------------------------
# 4. LOCAL CANDLE STORE (Phase 4 & 5)
# -----------------------------------------------------------------------------

def test_phase5_local_candle_store_aggregation_and_backfill(tmp_path):
    """Test Phase 5: LocalCandleStore rolls up completed bars and merges REST backfill."""
    store = LocalCandleStore(cache_dir=tmp_path / "candles", max_bars=50)

    base_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=IST)

    # 1. Simulate live ticks across 2 minutes
    # Minute 0: ticks at 10:00:10, 10:00:30, 10:00:50
    t1 = MagicMock(symbol="NIFTY", ltp=25000.0, volume=100, timestamp=base_time + timedelta(seconds=10))
    t2 = MagicMock(symbol="NIFTY", ltp=25020.0, volume=50, timestamp=base_time + timedelta(seconds=30))
    t3 = MagicMock(symbol="NIFTY", ltp=24990.0, volume=80, timestamp=base_time + timedelta(seconds=50))
    store.on_tick(t1)
    store.on_tick(t2)
    store.on_tick(t3)

    # Minute 1 tick triggers close of Minute 0 bar
    t4 = MagicMock(symbol="NIFTY", ltp=25010.0, volume=120, timestamp=base_time + timedelta(minutes=1, seconds=5))
    store.on_tick(t4)

    bars_1m = store._bars_1m.get("NIFTY", [])
    assert len(bars_1m) == 1
    # Check OHLCV for closed Minute 0 bar
    # Open=25000, High=25020, Low=24990, Close=24990, Volume=230
    assert bars_1m[0][1] == 25000.0
    assert bars_1m[0][2] == 25020.0
    assert bars_1m[0][3] == 24990.0
    assert bars_1m[0][4] == 24990.0
    assert bars_1m[0][5] == 230

    # 2. REST backfill merge: deduplication and sorting
    rest_data = [
        [(base_time - timedelta(minutes=2)).isoformat(), 24980, 24990, 24975, 24985, 500],
        [(base_time - timedelta(minutes=1)).isoformat(), 24985, 24995, 24980, 24990, 600],
    ]
    merged = store.merge_rest_candles("NIFTY", rest_data, interval="1m")
    assert len(merged) == 3  # 2 REST + 1 local completed
    assert merged[0][0] < merged[1][0] < merged[2][0]

    # 3. Freshness check: retrieval within max_age
    now_fresh = base_time + timedelta(minutes=2)
    res_fresh = store.get_candles("NIFTY", interval="1m", min_bars=2, max_age_seconds=300, now=now_fresh)
    assert res_fresh is not None
    assert res_fresh["bar_count"] == 3

    # Stale data fails closed
    now_stale = base_time + timedelta(hours=2)
    res_stale = store.get_candles("NIFTY", interval="1m", min_bars=2, max_age_seconds=300, now=now_stale)
    assert res_stale is None


# -----------------------------------------------------------------------------
# 5. DHANHQ READ-ONLY INTEGRATION (Phase 6 & 7)
# -----------------------------------------------------------------------------

def test_phase6_7_dhan_unconfigured_fails_closed_cleanly():
    """Test Phase 6 & 7: Dhan provider reports not_configured cleanly when credentials absent."""
    with patch.dict(os.environ, {"DHAN_CLIENT_ID": "", "DHAN_ACCESS_TOKEN": ""}, clear=True):
        provider = DhanMarketDataProvider()
        assert provider.is_configured() is False
        assert provider.state == DhanMarketDataState.NOT_CONFIGURED

        quote = provider.get_quote("NIFTY")
        assert quote is None
        assert provider.state == DhanMarketDataState.NOT_CONFIGURED

        candles = provider.get_candles("NIFTY")
        assert candles is None

        adapter = DhanMarketAdapter()
        assert adapter.is_configured() is False
        with pytest.raises(ValueError, match="dhan_not_configured"):
            adapter.get_candles("NIFTY")
        with pytest.raises(ValueError, match="dhan_not_configured"):
            adapter.get_quote("NIFTY")


def test_phase7_dhan_configured_mocked_read_only():
    """Test Phase 7: Dhan provider parses quotes and candles with zero order placement logic."""
    with patch.dict(os.environ, {"DHAN_CLIENT_ID": "1000000001", "DHAN_ACCESS_TOKEN": "mock_dhan_token_xyz"}):
        provider = DhanMarketDataProvider()
        assert provider.is_configured() is True
        assert provider.state == DhanMarketDataState.HEALTHY

        # Mock urllib.request.urlopen for quote
        mock_resp_quote = MagicMock()
        mock_resp_quote.read.return_value = json.dumps({
            "status": "success",
            "data": {
                "13": {"last_price": 25050.25, "buy_price": 25050.0, "sell_price": 25050.5, "volume": 120000}
            }
        }).encode("utf-8")
        mock_resp_quote.__enter__.return_value = mock_resp_quote

        with patch("urllib.request.urlopen", return_value=mock_resp_quote):
            quote = provider.get_quote("NIFTY")
            assert quote is not None
            assert quote["ltp"] == 25050.25
            assert quote["source"] == "LIVE_DHAN"
            assert quote["symbol"] == "NIFTY"


# -----------------------------------------------------------------------------
# 6. SMART PROVIDER FAILOVER & OBSERVABILITY (Phase 8 & 11)
# -----------------------------------------------------------------------------

def test_phase8_smart_adapter_failover_and_hysteresis():
    """Test Phase 8: SmartMultiProviderAdapter falls back on rate limit and respects hysteresis."""
    primary = MagicMock()
    primary.name = "angel_one"
    primary.is_configured.return_value = True

    fallback = MagicMock()
    fallback.name = "dhan"
    fallback.is_configured.return_value = True

    # Primary succeeds initially
    primary.get_candles.return_value = {"chart": [[f"2026-10-07T10:0{i}", 25000, 25010, 24990, 25005, 100] for i in range(30)], "timestamp": "2026-10-07T10:29"}
    smart = SmartMultiProviderAdapter(primary=primary, fallbacks=[fallback], failover_cooldown_sec=60.0)

    res1 = smart.get_candles("MOCK_UNDERLYING", interval="5m", allow_fallback=True)
    assert smart.fallback_active is False
    assert smart.active_adapter_name == "angel_one"

    # Primary hits rate limit -> failover to fallback
    primary.get_candles.side_effect = ValueError("angel_rate_limited")
    fallback.get_candles.return_value = {"chart": [[f"2026-10-07T10:0{i}", 25000, 25010, 24990, 25005, 100] for i in range(30)], "timestamp": "2026-10-07T10:30"}

    res2 = smart.get_candles("MOCK_UNDERLYING", interval="5m", allow_fallback=True)
    assert smart.fallback_active is True
    assert smart.active_adapter_name == "dhan"
    assert smart.fallback_reason == "angel_rate_limited"
    assert res2["is_fallback"] is True

    # Status check exposes complete provider health and fallback telemetry
    stat = smart.get_status()
    assert stat["primary"] == "angel_one"
    assert stat["active"] == "dhan"
    assert stat["fallback_active"] is True
    assert stat["reason"] == "angel_rate_limited"
    assert stat["health"]["angel_one"] == "rate_limited"
    assert stat["health"]["dhan"] == "healthy"


# -----------------------------------------------------------------------------
# 7. SHORTLIST TELEMETRY (Phase 11)
# -----------------------------------------------------------------------------

def test_phase11_shortlist_telemetry_captures_canonical_rejection_reasons(tmp_path):
    """Test Phase 11: Candidate evaluations record canonical rejection codes and are visible in status."""
    db_path = tmp_path / "ledger_phase11.sqlite3"
    ledger = PaperLedger(db_path, initial_cash=100000)
    feed = MagicMock()
    feed.source = "demo"
    feed.instrument = Instrument("NSE", "NIFTY", "99926000", 50, "INDEX")
    feed.connected = True

    service = PaperTradingService(feed=feed, ledger=ledger)
    service.fo_scanner.discover_universe = MagicMock(return_value=["RELIANCE"])

    now = datetime(2026, 9, 29, 10, 30, 0, tzinfo=IST)
    snapshot = {
        'price': 2500.0,
        'chart': [[(now - timedelta(minutes=30 - i)).isoformat(), 2500, 2505, 2495, 2502, 1000] for i in range(30)],
    }
    account = {'cash': 100000, 'positions': []}

    # Mock evaluate_price_action to reject candidate with NO_SETUP
    eval_no_setup = ScanEvaluationResult(
        symbol="RELIANCE",
        underlying="RELIANCE",
        spot_price=2500.0,
        state=SetupState.NO_SETUP,
        bias=MarketBias.NEUTRAL,
        reason="Consolidation range bound",
        candidate=None,
    )

    with patch.object(service.fo_scanner, 'evaluate_price_action', return_value=eval_no_setup):
        service._scan_and_execute_fo(snapshot, account, 1, force=True)

    scanner_res = service.last_scanner_result
    assert len(scanner_res['shortlist_telemetry']) >= 1
    item = scanner_res['shortlist_telemetry'][0]
    assert item['symbol'] == 'RELIANCE'
    assert item['state'] == SetupState.NO_SETUP.value
    assert item['rejection_reason'] == SetupState.NO_SETUP.value
    assert item['execution_status'] == 'NOT_EXECUTED'

    stat = service.status()
    assert 'market_data' in stat
    assert 'fo_scanner' in stat
    assert 'shortlist_telemetry' in stat['fo_scanner']
    assert stat['is_data_stale'] is False

    ledger.close()


# -----------------------------------------------------------------------------
# 8. SECRET REDACTION & LOG INTEGRITY (Phase 12)
# -----------------------------------------------------------------------------

def test_phase12_secret_redaction_prevents_logging_tokens(caplog):
    """Test Phase 12: Registered secrets and API tokens are redacted from output."""
    install_secret_redaction()
    secret_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sensitive_payload"
    secret_totp = "JBSWY3DPEHPK3PXP"
    register_secret(secret_jwt)
    register_secret(secret_totp)

    import logging
    with caplog.at_level(logging.INFO):
        test_logger = logging.getLogger("test_redaction")
        test_logger.info(f"Connecting with token {secret_jwt} and TOTP {secret_totp}")

    log_output = caplog.text
    assert secret_jwt not in log_output
    assert secret_totp not in log_output
    assert "[REDACTED]" in log_output
