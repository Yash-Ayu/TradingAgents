"""
Deterministic unit and integration test suite for Autonomous Indian F&O Runtime Scanner.
Covers all 20 required production safety and architecture criteria:
1. Dynamic universe from Scrip Master used in production runtime.
2. Old hard-coded list (priority_symbols) completely absent from scanner selection path.
3. Non-hardcoded stocks reach screening and evaluation.
4. scanner_batch_size is strictly respected.
5. Systematic rotation across consecutive cycles covers different stocks.
6. Rotation eventually covers entire dynamic universe without starvation.
7. Per-symbol real candle data isolation (no chart leakage from dashboard instrument).
8. Consumes candles['chart'] response schema correctly.
9. Missing market data fails closed.
10. Stale market data fails closed.
11. Flat / dead market data (std=0 or range=0) fails closed.
12. Malformed / broken OHLC data fails closed.
13. Fallback/research provenance cannot masquerade as live Angel data.
14. Expensive analysis is not called for every universe member.
15. Telemetry fields populated with complete, consistent counts.
16. Successful paper execution does not leave telemetry stale.
17. Reference --symbol does not constrain the autonomous scanner.
18. BTST engine uses safe dynamic architecture.
19. Intraday scanner uses safe dynamic architecture.
20. Paper-only safety invariants preserved with zero real broker execution.
"""

import inspect
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from tradingagents.runtime.market_snapshot import IST, Instrument
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService


class MockScripMasterDynamic:
    """Configurable mock Scrip Master for deterministic F&O universe tests."""

    def __init__(self, universe_map=None):
        if universe_map is None:
            self._universe = {
                "NIFTY": {"underlying": "NIFTY", "exchange": "NFO", "is_index": True, "lot_size": 25, "expiries": ["26SEP2024"]},
                "BANKNIFTY": {"underlying": "BANKNIFTY", "exchange": "NFO", "is_index": True, "lot_size": 15, "expiries": ["26SEP2024"]},
                "DIXON": {"underlying": "DIXON", "exchange": "NFO", "is_index": False, "lot_size": 100, "expiries": ["26SEP2024"]},
                "POLYCAB": {"underlying": "POLYCAB", "exchange": "NFO", "is_index": False, "lot_size": 125, "expiries": ["26SEP2024"]},
                "ZYDUSLIFE": {"underlying": "ZYDUSLIFE", "exchange": "NFO", "is_index": False, "lot_size": 900, "expiries": ["26SEP2024"]},
                "FEDERALBNK": {"underlying": "FEDERALBNK", "exchange": "NFO", "is_index": False, "lot_size": 5000, "expiries": ["26SEP2024"]},
                "ABCAPITAL": {"underlying": "ABCAPITAL", "exchange": "NFO", "is_index": False, "lot_size": 5400, "expiries": ["26SEP2024"]},
            }
        else:
            self._universe = dict(universe_map)

    def get_fo_universe(self, exchange: str = "NFO"):
        return dict(self._universe)

    def find_option_contract(self, underlying, exchange, expiry, strike, option_type, instrument_type=None):
        rec = MagicMock()
        rec.symbol = f"{underlying}24SEP{int(strike)}{option_type}"
        rec.trading_symbol = rec.symbol
        rec.token = "12345"
        rec.lotsize = self._universe.get(underlying, {}).get("lot_size", 50)
        rec.strike = strike
        rec.expiry = expiry
        return rec

    def get_expiries(self, underlying, exchange, instrument_type="OPTIDX", filter_unexpired=True):
        return ["26SEP2024"]

    def get_available_strikes(self, underlying, exchange, expiry, option_type=None, instrument_type=None):
        return [2000.0, 2050.0, 2100.0, 24000.0, 24500.0]


def _build_dummy_feed(symbol="NIFTY", exchange="NSE", source="demo"):
    feed = MagicMock()
    feed.source = source
    inst = Instrument(exchange, symbol, "99926000", lot_size=25, instrument_type="EQ")
    feed.instrument = inst
    feed.connected = True
    feed.snapshot.return_value = {
        'source': source,
        'instrument': inst.as_dict(),
        'price': 24000.0,
        'vix': 13.5,
        'atr': 120.0,
        'atr_ratio': 1.0,
        'trend_strength': 0.8,
        'timestamp': datetime.now(IST).isoformat(),
        'vix_timestamp': datetime.now(IST).isoformat(),
        'bar_timestamp': datetime.now(IST).replace(second=0, microsecond=0).isoformat(),
        'chart': _generate_valid_candles(base_price=24000.0, n_bars=30)
    }
    feed.fetch.return_value = feed.snapshot.return_value
    return feed


def _generate_valid_candles(base_price=1000.0, n_bars=30, base_time=None):
    rows = []
    start = base_time or datetime.now(IST) - timedelta(minutes=5 * n_bars)
    for i in range(n_bars):
        t = (start + timedelta(minutes=5 * i)).isoformat()
        bar_o = base_price + (i % 3) * 2.0
        bar_h = bar_o + 8.0
        bar_l = bar_o - 4.0
        bar_c = bar_o + 3.0
        bar_v = 5000 + i * 150
        rows.append([t, bar_o, bar_h, bar_l, bar_c, bar_v])
    return rows


def _make_service(tmp_path, scrip_master=None, batch_size=12, feed=None):
    feed = feed or _build_dummy_feed()
    ledger = PaperLedger(tmp_path / "paper_test.sqlite3", initial_cash=100000.0)
    service = PaperTradingService(
        feed, ledger, scrip_master=scrip_master, scanner_batch_size=batch_size
    )
    return service, feed, ledger


# --------------------------------------------------------------------------
# Test 1 & 2: Dynamic Universe Discovery and Absence of Hardcoded Priority List
# --------------------------------------------------------------------------

def test_dynamic_universe_production_runtime_used(tmp_path):
    """Test 1: ScripMaster.get_fo_universe() is dynamically queried with variable counts."""
    dynamic_universe = {f"TICKER_{i}": {"underlying": f"TICKER_{i}", "is_index": False, "lot_size": 50} for i in range(27)}
    dynamic_universe["INDEX_X"] = {"underlying": "INDEX_X", "is_index": True, "lot_size": 25}

    mock_sm = MockScripMasterDynamic(dynamic_universe)
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert service.last_scanner_result["universe_count"] == 28
    assert service.last_scanner_result["batch_size"] == 12


def test_old_hardcoded_priority_symbols_absent():
    """Test 2: Ensure hardcoded priority_symbols list is not in paper_service._scan_and_execute_fo."""
    src = inspect.getsource(PaperTradingService._scan_and_execute_fo)
    assert 'priority_symbols = ["NIFTY"' not in src
    assert 'priority_symbols[:4]' not in src


# --------------------------------------------------------------------------
# Test 3: Non-hardcoded stocks reach screening
# --------------------------------------------------------------------------

def test_non_hardcoded_stock_reaches_screening(tmp_path):
    """Test 3: Non-hardcoded stocks like DIXON and POLYCAB reach screening and evaluation."""
    mock_sm = MockScripMasterDynamic()
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm, batch_size=10)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {
        'chart': _generate_valid_candles(base_price=9000.0, n_bars=30),
        'source': 'DEMO_SYNTHETIC',
    }

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    evaluated_symbols = [e['symbol'] for e in service.last_scanner_result['evaluations']]
    assert any(s in evaluated_symbols for s in ("DIXON", "POLYCAB", "ZYDUSLIFE", "FEDERALBNK", "ABCAPITAL"))


# --------------------------------------------------------------------------
# Test 4, 5 & 6: Bounded Workload and Systematic Rotation
# --------------------------------------------------------------------------

def test_batch_size_strictly_respected(tmp_path):
    """Test 4: Batch size is strictly bounded in each cycle."""
    custom_u = {f"SYM_{i:02d}": {"underlying": f"SYM_{i:02d}", "is_index": False, "lot_size": 100} for i in range(25)}
    mock_sm = MockScripMasterDynamic(custom_u)
    batch_size = 7
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm, batch_size=batch_size)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': _generate_valid_candles(100.0, 30), 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert service.last_scanner_result['screened_count'] <= batch_size
    assert len(service.last_scanner_result['evaluations']) <= batch_size


def test_rotation_consecutive_cycles_covers_different_symbols(tmp_path):
    """Test 5: Consecutive cycles screen different non-index symbols without duplication."""
    custom_u = {f"STOCK_{i:02d}": {"underlying": f"STOCK_{i:02d}", "is_index": False, "lot_size": 100} for i in range(20)}
    custom_u["NIFTY"] = {"underlying": "NIFTY", "is_index": True, "lot_size": 25}
    mock_sm = MockScripMasterDynamic(custom_u)

    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm, batch_size=6)
    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': _generate_valid_candles(200.0, 30), 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)
        cycle1_stocks = {e['symbol'] for e in service.last_scanner_result['evaluations'] if e['symbol'] != 'NIFTY'}

        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=2)
        cycle2_stocks = {e['symbol'] for e in service.last_scanner_result['evaluations'] if e['symbol'] != 'NIFTY'}

    assert cycle1_stocks != cycle2_stocks
    assert len(cycle1_stocks.intersection(cycle2_stocks)) == 0


def test_rotation_eventually_covers_entire_universe(tmp_path):
    """Test 6: Over multiple consecutive cycles, rotation eventually covers the entire universe."""
    universe_size = 15
    custom_u = {f"EQ_{i:02d}": {"underlying": f"EQ_{i:02d}", "is_index": False, "lot_size": 100} for i in range(universe_size)}
    mock_sm = MockScripMasterDynamic(custom_u)

    batch_size = 5
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm, batch_size=batch_size)
    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': _generate_valid_candles(300.0, 30), 'source': 'DEMO_SYNTHETIC'}

    all_screened = set()
    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        for gen in range(1, 4):  # 3 cycles * 5 per cycle = 15 symbols
            service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=gen)
            for e in service.last_scanner_result['evaluations']:
                all_screened.add(e['symbol'])

    assert len(all_screened) == universe_size
    assert all_screened == set(custom_u.keys())


# --------------------------------------------------------------------------
# Test 7 & 8: Per-Symbol Isolation and candles['chart'] Schema Consumption
# --------------------------------------------------------------------------

def test_per_symbol_real_market_data_isolation(tmp_path):
    """Test 7: Symbol B never reuses Symbol A's snapshot chart; isolated candles are fetched per symbol."""
    mock_sm = MockScripMasterDynamic()
    feed = _build_dummy_feed(symbol="NIFTY")
    service, _, ledger = _make_service(tmp_path, scrip_master=mock_sm, feed=feed)

    polycab_candles = _generate_valid_candles(base_price=6500.0, n_bars=30)
    dixon_candles = _generate_valid_candles(base_price=12000.0, n_bars=30)

    queried_symbols = []

    def mock_get(sym, interval='5m'):
        queried_symbols.append(sym)
        if sym == "POLYCAB":
            return {'chart': polycab_candles, 'source': 'DEMO_SYNTHETIC'}
        return {'chart': dixon_candles, 'source': 'DEMO_SYNTHETIC'}

    mock_adapter = MagicMock()
    mock_adapter.get_candles.side_effect = mock_get

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert "POLYCAB" in queried_symbols
    assert "DIXON" in queried_symbols
    # Primary instrument NIFTY was from snapshot, not queried via external adapter
    assert "NIFTY" not in queried_symbols


def test_chart_schema_correctly_consumed(tmp_path):
    """Test 8: Scanner correctly extracts candles['chart'] with [timestamp, open, high, low, close, volume]."""
    mock_sm = MockScripMasterDynamic({"POLYCAB": {"underlying": "POLYCAB", "is_index": False, "lot_size": 125}})
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm, batch_size=5)

    valid_rows = _generate_valid_candles(6000.0, 30)
    mock_adapter = MagicMock()
    # Explicitly verify 'chart' key is consumed
    mock_adapter.get_candles.return_value = {'chart': valid_rows, 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert service.last_scanner_result['shortlisted_count'] >= 1
    eval_poly = [e for e in service.last_scanner_result['evaluations'] if e['symbol'] == 'POLYCAB']
    assert len(eval_poly) == 1
    assert eval_poly[0]['reason'] != "Market data unavailable or malformed. Fail-closed."


# --------------------------------------------------------------------------
# Test 9, 10, 11, 12, 13: Data Quality Gates (Fail-Closed) & Provenance
# --------------------------------------------------------------------------

def test_missing_market_data_fails_closed(tmp_path):
    """Test 9: Missing or empty candles fail closed and increment data_unavailable_count."""
    mock_sm = MockScripMasterDynamic({"DIXON": {"underlying": "DIXON", "is_index": False, "lot_size": 100}})
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': [], 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert service.last_scanner_result['data_unavailable_count'] >= 1
    assert service.last_scanner_result['shortlisted_count'] == 0


def test_stale_market_data_fails_closed(tmp_path):
    """Test 10: In live Angel mode, candles older than 15 min during market hours fail closed."""
    mock_sm = MockScripMasterDynamic({"POLYCAB": {"underlying": "POLYCAB", "is_index": False, "lot_size": 125}})
    feed = _build_dummy_feed(source="angel")
    service, _, ledger = _make_service(tmp_path, scrip_master=mock_sm, feed=feed)

    now = datetime(2026, 9, 28, 11, 30, tzinfo=IST)
    service.clock = lambda: now
    service.calendar = MagicMock()
    service.calendar.is_open.return_value = True

    # 30 bars ending 1 hour ago (stale)
    stale_time = now - timedelta(hours=1) - timedelta(minutes=5 * 30)
    stale_rows = _generate_valid_candles(5000.0, 30, base_time=stale_time)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': stale_rows, 'source': 'LIVE_ANGEL_ONE'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert service.last_scanner_result['data_unavailable_count'] >= 1
    evals = service.last_scanner_result['evaluations']
    assert any("stale_candle_data" in e['reason'] for e in evals)


def test_flat_dead_feed_fails_closed(tmp_path):
    """Test 11: Completely flat prices (std = 0) are rejected as dead feed."""
    mock_sm = MockScripMasterDynamic({"DEAD_STOCK": {"underlying": "DEAD_STOCK", "is_index": False, "lot_size": 50}})
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    base_time = datetime.now(IST) - timedelta(minutes=150)
    flat_rows = [[(base_time + timedelta(minutes=5 * i)).isoformat(), 100.0, 100.0, 100.0, 100.0, 1000] for i in range(30)]

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': flat_rows, 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    evals = service.last_scanner_result['evaluations']
    assert any("flat_dead_feed_zero_std" in e['reason'] for e in evals)
    assert service.last_scanner_result['shortlisted_count'] == 0


def test_malformed_ohlc_fails_closed(tmp_path):
    """Test 12: Broken OHLC hierarchy (High < Low) fails closed."""
    mock_sm = MockScripMasterDynamic({"BROKEN_STOCK": {"underlying": "BROKEN_STOCK", "is_index": False, "lot_size": 50}})
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    broken_rows = _generate_valid_candles(500.0, 30)
    broken_rows[-1][2] = 400.0  # High lower than Low (invalid)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': broken_rows, 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    evals = service.last_scanner_result['evaluations']
    assert any("malformed_ohlc_relationship" in e['reason'] for e in evals)


def test_fallback_provenance_rejected_in_angel_mode(tmp_path):
    """Test 13: In Angel live mode, fallback/research data masquerading as Angel is rejected."""
    mock_sm = MockScripMasterDynamic({"ANGEL_TEST": {"underlying": "ANGEL_TEST", "is_index": False, "lot_size": 50}})
    feed = _build_dummy_feed(source="angel")
    service, _, ledger = _make_service(tmp_path, scrip_master=mock_sm, feed=feed)

    mock_adapter = MagicMock()
    # Masquerading fallback
    mock_adapter.get_candles.return_value = {
        'chart': _generate_valid_candles(1500.0, 30),
        'source': 'YAHOO_FINANCE_RESEARCH'
    }

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    evals = service.last_scanner_result['evaluations']
    assert any("provenance_rejected_YAHOO_FINANCE_RESEARCH" in e['reason'] for e in evals)
    assert service.last_scanner_result['data_unavailable_count'] >= 1


# --------------------------------------------------------------------------
# Test 14 & 15: Telemetry and Analysis Budget Invariants
# --------------------------------------------------------------------------

def test_deep_analysis_not_called_for_every_universe_member(tmp_path):
    """Test 14: Expensive analysis/orchestrator is only called for shortlisted actionable setups."""
    mock_sm = MockScripMasterDynamic()
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    # All candles stable consolidation without trigger
    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': _generate_valid_candles(2000.0, 30), 'source': 'DEMO_SYNTHETIC'}

    with patch.object(service.fo_orchestrator, 'process_signal') as mock_process:
        with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
            service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)
        # Should NOT process signals when no setup is triggered
        assert mock_process.call_count == 0


def test_telemetry_fields_populated_with_correct_counts(tmp_path):
    """Test 15: All 9 core telemetry fields and rotation indices are correctly populated."""
    mock_sm = MockScripMasterDynamic()
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm, batch_size=5)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': _generate_valid_candles(1000.0, 30), 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    res = service.last_scanner_result
    assert res["universe_count"] == 7
    assert res["screened_count"] == 5
    assert res["shortlisted_count"] <= res["screened_count"]
    assert res["screened_count"] == len(res["active_candidates"]) + res["rejected_count"]
    assert "scan_duration" in res and res["scan_duration"] >= 0.0
    assert "last_scan_timestamp" in res and res["last_scan_timestamp"] is not None
    assert "rotation_index" in res
    assert "next_rotation_index" in res
    assert "rejected_by_reason" in res


# --------------------------------------------------------------------------
# Test 16 & 17: Execution Telemetry and CLI --symbol Independence
# --------------------------------------------------------------------------

def test_successful_execution_does_not_leave_telemetry_stale(tmp_path):
    """Test 16: Telemetry is finalized and updated before returning executed order result."""
    mock_sm = MockScripMasterDynamic({"TRIGGER_STOCK": {"underlying": "TRIGGER_STOCK", "is_index": False, "lot_size": 100}})
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm, batch_size=5)

    # Construct hammer trigger candle series
    rows = _generate_valid_candles(1000.0, 30)
    # Hammer shape on last bar
    rows[-1][1] = 1000.0  # Open
    rows[-1][2] = 1005.0  # High
    rows[-1][3] = 960.0   # Low (long rejection lower wick)
    rows[-1][4] = 1003.0  # Close
    rows[-1][5] = 25000   # Volume spike

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': rows, 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    # Telemetry was updated even if execution occurred
    assert service.last_scanner_result["last_scan_timestamp"] is not None
    assert service.last_scanner_result["screened_count"] >= 1


def test_symbol_cli_arg_does_not_constrain_dynamic_scanner(tmp_path):
    """Test 17: Reference instrument (e.g. SBIN) does not constrain scanner to only SBIN."""
    mock_sm = MockScripMasterDynamic()
    feed = _build_dummy_feed(symbol="SBIN")  # CLI set to SBIN
    service, _, ledger = _make_service(tmp_path, scrip_master=mock_sm, feed=feed)

    scanned_symbols = []

    def mock_get(sym, interval='5m'):
        scanned_symbols.append(sym)
        return {'chart': _generate_valid_candles(2000.0, 30), 'source': 'DEMO_SYNTHETIC'}

    mock_adapter = MagicMock()
    mock_adapter.get_candles.side_effect = mock_get

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    # Scanner scanned broad F&O universe, not restricted to SBIN
    assert any(s in scanned_symbols for s in ("NIFTY", "BANKNIFTY", "DIXON", "POLYCAB"))


# --------------------------------------------------------------------------
# Test 18, 19 & 20: BTST, Intraday, and Paper Safety Invariants
# --------------------------------------------------------------------------

def test_btst_uses_safe_dynamic_architecture(tmp_path):
    """Test 18: In BTST window (14:45 - 15:15 IST), BTST evaluates candidates from dynamic screened batch."""
    mock_sm = MockScripMasterDynamic({"BTST_CAND": {"underlying": "BTST_CAND", "is_index": False, "lot_size": 100}})
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    # Set clock to 15:00 IST (inside BTST window)
    service.clock = lambda: datetime(2026, 9, 28, 15, 0, tzinfo=IST)

    rows = _generate_valid_candles(1500.0, 35)
    # Strong close near day high
    rows[-1][1] = 1520.0
    rows[-1][2] = 1550.0
    rows[-1][3] = 1515.0
    rows[-1][4] = 1548.0
    rows[-1][5] = 20000

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': rows, 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert service.last_btst_result["window_active"] is True
    assert service.last_scanner_result["screened_count"] >= 1


def test_intraday_uses_safe_dynamic_architecture(tmp_path):
    """Test 19: Outside BTST window, intraday scanner safely screens dynamic universe."""
    mock_sm = MockScripMasterDynamic({"INTRADAY_CAND": {"underlying": "INTRADAY_CAND", "is_index": False, "lot_size": 50}})
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    # 10:30 IST (outside BTST window)
    service.clock = lambda: datetime(2026, 9, 28, 10, 30, tzinfo=IST)

    mock_adapter = MagicMock()
    mock_adapter.get_candles.return_value = {'chart': _generate_valid_candles(1000.0, 30), 'source': 'DEMO_SYNTHETIC'}

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=mock_adapter):
        service._scan_and_execute_fo(feed.snapshot(), ledger.account(), generation=1)

    assert service.last_btst_result["window_active"] is False
    assert service.last_scanner_result["screened_count"] >= 1


def test_paper_safety_invariants_strictly_preserved(tmp_path):
    """Test 20: System mode is strictly 'paper' and real broker write APIs are completely absent/uncalled."""
    mock_sm = MockScripMasterDynamic()
    service, feed, ledger = _make_service(tmp_path, scrip_master=mock_sm)

    st = service.status()
    assert st["mode"] == "paper"
    assert st["fo_positions"] == []
    assert st["fo_orders"] == []

    # Verify real broker write methods are not callable on paper engine or orchestrator
    assert not hasattr(service.fo_orchestrator.paper_engine, 'placeOrder')
    assert not hasattr(service.fo_orchestrator.paper_engine, 'modifyOrder')
    assert not hasattr(service.fo_orchestrator.paper_engine, 'cancelOrder')
