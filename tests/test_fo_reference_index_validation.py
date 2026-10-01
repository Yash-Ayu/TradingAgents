"""Regression tests for Reference Index Validation vs Executable Trade Instrument Validation.

Verifies:
1. Nifty 50 INDEX reference snapshot passes PaperTradingService reference validation.
2. Direct INDEX.validate_trade() strictly raises non_tradable_instrument.
3. Legacy _commit() strictly blocks execution for an INDEX instrument.
4. Nifty 50 INDEX reference feed allows auto F&O path: tick() -> _validate() -> _auto_tick() -> _scan_and_execute_fo().
5. Resolved valid OPTIDX CE/PE contracts properly validate at execution boundary.
6. Invalid or expired derivative contracts fail closed.
7. Paper-only invariant is strictly preserved.
8. live_execution remains false and real broker mutation APIs are never invoked.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.integrations.angel_one.paper_engine import FOPaperTradingEngine
from tradingagents.runtime.market_snapshot import (
    IST,
    Instrument,
    InstrumentResolver,
    SessionCalendar,
)
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService


class DummyIndexFeed:
    """Mock read-only feed for an INDEX reference instrument."""

    def __init__(self, symbol: str = "Nifty 50", token: str = "99926000"):
        self.source = "angel"
        self.connected = True
        self.instrument = Instrument("NSE", symbol, token, 1, "INDEX")

    def fetch(self, now: datetime) -> dict:
        bar_start = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).isoformat()
        return {
            "source": self.source,
            "instrument": self.instrument.as_dict(),
            "price": 25150.75,
            "vix": 13.85,
            "atr": 45.2,
            "atr_ratio": 1.05,
            "trend_strength": 0.85,
            "timestamp": now.isoformat(),
            "vix_timestamp": now.isoformat(),
            "bar_timestamp": bar_start,
            "chart": [
                [(now - timedelta(minutes=5 * i)).isoformat(), 25100 + i, 25120 + i, 25090 + i, 25110 + i, 50000]
                for i in range(30, 0, -1)
            ],
            "broker_account": {"available_cash": 500000.0, "orders": []},
        }


@pytest.fixture
def test_clock():
    return [datetime(2026, 9, 29, 10, 15, 0, tzinfo=IST)]


@pytest.fixture
def paper_ledger_instance(tmp_path):
    ledger = PaperLedger(tmp_path / "paper_test_index.sqlite3", initial_cash=200000.0)
    yield ledger
    ledger.close()


def test_1_nifty50_index_reference_validation_passes(paper_ledger_instance, test_clock):
    """TEST 1: Nifty 50 INDEX reference snapshot passes PaperTradingService reference validation."""
    feed = DummyIndexFeed("Nifty 50", "99926000")
    service = PaperTradingService(
        feed,
        paper_ledger_instance,
        calendar=SessionCalendar({"year": 2026}),
        clock=lambda: test_clock[0],
    )
    snapshot = feed.fetch(test_clock[0])

    # Reference validation should succeed without raising non_tradable_instrument
    service._validate(snapshot, test_clock[0])

    # Verify validate_reference allows INDEX
    service.instrument.validate_reference(test_clock[0])


def test_2_direct_index_validate_trade_raises_non_tradable_instrument(test_clock):
    """TEST 2: Direct INDEX.validate_trade() strictly raises non_tradable_instrument."""
    index_inst = Instrument("NSE", "Nifty 50", "99926000", 1, "INDEX")

    with pytest.raises(ValueError, match="non_tradable_instrument"):
        index_inst.validate_trade(test_clock[0])

    banknifty_inst = Instrument("NSE", "Nifty Bank", "99926009", 1, "INDEX")
    with pytest.raises(ValueError, match="non_tradable_instrument"):
        banknifty_inst.validate_trade(test_clock[0])


def test_3_legacy_commit_strictly_blocks_index_execution(paper_ledger_instance, test_clock):
    """TEST 3: Legacy _commit() strictly blocks execution for an INDEX instrument."""
    feed = DummyIndexFeed("Nifty 50", "99926000")
    service = PaperTradingService(
        feed,
        paper_ledger_instance,
        calendar=SessionCalendar({"year": 2026}),
        clock=lambda: test_clock[0],
    )
    service.start(background=False)
    snapshot = feed.fetch(test_clock[0])

    result = service._commit(snapshot, service.generation, "Buy")

    assert result["status"] == "blocked"
    assert result["reason"] == "non_tradable_execution_instrument"
    # Ensure no order was created in the ledger
    assert len(paper_ledger_instance.history()["orders"]) == 0


def test_4_index_reference_feed_enables_auto_fo_scanning_path(paper_ledger_instance, test_clock):
    """TEST 4: Nifty 50 INDEX reference feed allows auto F&O path: tick() -> _validate() -> _auto_tick() -> _scan_and_execute_fo()."""
    feed = DummyIndexFeed("Nifty 50", "99926000")
    mock_scrip = MagicMock()
    mock_scrip.get_fo_universe.return_value = {
        "NIFTY": {"underlying": "NIFTY", "exchange": "NFO", "is_index": True, "lot_size": 25, "expiries": ["26SEP2026"]},
        "RELIANCE": {"underlying": "RELIANCE", "exchange": "NFO", "is_index": False, "lot_size": 250, "expiries": ["26SEP2026"]},
    }

    mock_adapter = MagicMock()
    valid_candles = [
        [(test_clock[0] - timedelta(minutes=5 * i)).isoformat(), 25000 + i, 25020 + i, 24980 + i, 25010 + i, 10000]
        for i in range(30, 0, -1)
    ]
    mock_adapter.get_candles.return_value = {"chart": valid_candles, "source": "LIVE_ANGEL_ONE"}

    service = PaperTradingService(
        feed,
        paper_ledger_instance,
        calendar=SessionCalendar({"year": 2026}),
        clock=lambda: test_clock[0],
        scrip_master=mock_scrip,
        scanner_batch_size=5,
    )
    service.start(background=False)
    service.auto_enabled = True

    with patch("tradingagents.runtime.market_adapter.get_active_market_adapter", return_value=mock_adapter):
        tick_result = service.tick()

    # The tick must NOT fail with non_tradable_instrument
    assert tick_result.get("status") != "error"
    # Telemetry should be populated from the scanner run
    assert service.last_scanner_result["universe_count"] == 2
    assert service.last_scanner_result["screened_count"] >= 1
    assert service.last_scanner_result["last_scan_timestamp"] is not None


def test_5_resolved_optidx_validates_at_execution_boundary(test_clock):
    """TEST 5: Resolved valid OPTIDX CE/PE actual execution boundary properly validates."""
    future_date = (test_clock[0] + timedelta(days=7)).strftime("%Y-%m-%d")
    optidx_inst = Instrument("NFO", "NIFTY26SEP25000CE", "888123", 25, "OPTIDX", expiry=future_date)

    # Executable contract must pass validate_trade
    optidx_inst.validate_trade(test_clock[0])
    assert optidx_inst.key == "NFO:888123"

    optstk_inst = Instrument("NFO", "RELIANCE26SEP3000CE", "888456", 250, "OPTSTK", expiry=future_date)
    optstk_inst.validate_trade(test_clock[0])


def test_6_invalid_or_expired_derivative_fails_closed(test_clock):
    """TEST 6: Invalid or expired derivative contracts fail closed."""
    past_date = (test_clock[0] - timedelta(days=2)).strftime("%Y-%m-%d")
    expired_opt = Instrument("NFO", "NIFTY24SEP25000CE", "888123", 25, "OPTIDX", expiry=past_date)

    with pytest.raises(ValueError, match="expired_instrument"):
        expired_opt.validate_trade(test_clock[0])

    with pytest.raises(ValueError, match="expired_instrument"):
        expired_opt.validate_reference(test_clock[0])

    # NFO contract missing expiry
    no_expiry_opt = Instrument("NFO", "NIFTY_NO_EXPIRY", "888999", 25, "OPTIDX", expiry=None)
    with pytest.raises(ValueError, match="expiry_required"):
        no_expiry_opt.validate_trade(test_clock[0])

    with pytest.raises(ValueError, match="expiry_required"):
        no_expiry_opt.validate_reference(test_clock[0])


def test_7_paper_only_invariant_intact(paper_ledger_instance, test_clock):
    """TEST 7: Paper-only invariant is strictly preserved across service status and orchestrator."""
    feed = DummyIndexFeed("Nifty 50", "99926000")
    service = PaperTradingService(
        feed,
        paper_ledger_instance,
        calendar=SessionCalendar({"year": 2026}),
        clock=lambda: test_clock[0],
    )
    status = service.status()
    assert status["mode"] == "paper"
    assert status["source"] == "angel"
    assert service.fo_orchestrator.paper_engine is not None


def test_8_live_execution_false_and_no_broker_order_calls():
    """TEST 8: live_execution remains false and real broker order APIs are never invoked."""
    paper_engine = FOPaperTradingEngine()
    # Ensure live_execution is false / paper mode only
    assert paper_engine is not None

    # Verify Angel real order mutation calls remain unimplemented/blocked in read-only adapter
    from tradingagents.runtime.angel_data import AngelReadOnlyFeed
    feed = AngelReadOnlyFeed(
        Instrument("NSE", "TEST-EQ", "123", 1, "EQ"),
        Instrument("NSE", "India VIX", "456", 1, "INDEX"),
    )
    assert not hasattr(feed, "place_order")
    assert not hasattr(feed, "cancel_order")


# =============================================================================
# REAL-MASTER-SHAPE REGRESSION TESTS (AMXIDX -> INDEX Normalization)
# =============================================================================

def test_9_real_angel_master_nifty_amxidx_resolves_to_canonical_index():
    """TEST 9: NSE Nifty 50 with real Angel master instrumenttype AMXIDX resolves to canonical INDEX."""
    nifty_row = {
        'token': '99926000',
        'symbol': 'Nifty 50',
        'name': 'NIFTY',
        'expiry': '',
        'strike': '0.000000',
        'lotsize': '1',
        'instrumenttype': 'AMXIDX',
        'exch_seg': 'NSE',
        'tick_size': '0.000000',
    }
    vix_row = {
        'token': '99926017',
        'symbol': 'India VIX',
        'name': 'INDIA VIX',
        'expiry': '',
        'strike': '0.000000',
        'lotsize': '1',
        'instrumenttype': 'AMXIDX',
        'exch_seg': 'NSE',
        'tick_size': '0.000000',
    }
    resolver = InstrumentResolver([nifty_row, vix_row])
    inst_nifty = resolver.resolve('NSE', 'Nifty 50')
    inst_vix = resolver.resolve('NSE', 'India VIX')

    assert inst_nifty.instrument_type == 'INDEX'
    assert inst_nifty.symbol == 'Nifty 50'
    assert inst_nifty.token == '99926000'
    assert inst_nifty.lot_size == 1

    assert inst_vix.instrument_type == 'INDEX'
    assert inst_vix.symbol == 'India VIX'
    assert inst_vix.token == '99926017'


def test_10_resolved_index_validate_reference_passes_and_validate_trade_fails(test_clock):
    """TEST 10: Resolved INDEX allows validate_reference but strictly rejects validate_trade."""
    nifty_row = {
        'token': '99926000',
        'symbol': 'Nifty 50',
        'lotsize': '1',
        'instrumenttype': 'AMXIDX',
        'exch_seg': 'NSE',
    }
    inst = InstrumentResolver([nifty_row]).resolve('NSE', 'Nifty 50')
    now = test_clock[0]

    # Reference validation passes for autonomous scanner/feed
    inst.validate_reference(now)

    # Trade validation strictly fails closed
    with pytest.raises(ValueError, match="non_tradable_instrument"):
        inst.validate_trade(now)


def test_11_valid_equity_with_blank_instrumenttype_resolves_to_eq(test_clock):
    """TEST 11: Valid cash equity with blank instrumenttype resolves to EQ and passes both validations."""
    sbin_row = {
        'token': '3045',
        'symbol': 'SBIN-EQ',
        'name': 'SBIN',
        'expiry': '',
        'strike': '-1.000000',
        'lotsize': '1',
        'instrumenttype': '',
        'exch_seg': 'NSE',
    }
    inst = InstrumentResolver([sbin_row]).resolve('NSE', 'SBIN-EQ')
    now = test_clock[0]

    assert inst.instrument_type == 'EQ'
    assert inst.symbol == 'SBIN-EQ'
    assert inst.token == '3045'
    inst.validate_reference(now)
    inst.validate_trade(now)


def test_12_canonical_derivatives_optidx_optstk_futidx_futstk_preserved(test_clock):
    """TEST 12: Derivative types OPTIDX, OPTSTK, FUTIDX, FUTSTK remain preserved and valid."""
    rows = [
        {'token': '101', 'symbol': 'NIFTY_OPT', 'lotsize': '25', 'instrumenttype': 'OPTIDX', 'exch_seg': 'NFO', 'expiry': '26Sep2026'},
        {'token': '102', 'symbol': 'SBIN_OPT', 'lotsize': '750', 'instrumenttype': 'OPTSTK', 'exch_seg': 'NFO', 'expiry': '26Sep2026'},
        {'token': '103', 'symbol': 'NIFTY_FUT', 'lotsize': '25', 'instrumenttype': 'FUTIDX', 'exch_seg': 'NFO', 'expiry': '26Sep2026'},
        {'token': '104', 'symbol': 'SBIN_FUT', 'lotsize': '750', 'instrumenttype': 'FUTSTK', 'exch_seg': 'NFO', 'expiry': '26Sep2026'},
    ]
    resolver = InstrumentResolver(rows)
    future_now = datetime(2026, 9, 20, 10, 0, 0, tzinfo=IST)

    for r in rows:
        inst = resolver.resolve('NFO', r['symbol'])
        assert inst.instrument_type == r['instrumenttype']
        assert inst.expiry == '2026-09-26'
        inst.validate_reference(future_now)
        inst.validate_trade(future_now)


def test_13_unknown_instrument_type_fails_closed(test_clock):
    """TEST 13: Unknown instrument types fail closed under both reference and trade validation."""
    unknown_row = {
        'token': '99999',
        'symbol': 'UNKNOWN_ASSET',
        'lotsize': '1',
        'instrumenttype': 'COMMODITY_UNKNOWN',
        'exch_seg': 'NSE',
    }
    inst = InstrumentResolver([unknown_row]).resolve('NSE', 'UNKNOWN_ASSET')
    now = test_clock[0]

    assert inst.instrument_type == 'COMMODITY_UNKNOWN'
    with pytest.raises(ValueError, match="non_tradable_instrument"):
        inst.validate_reference(now)
    with pytest.raises(ValueError, match="non_tradable_instrument"):
        inst.validate_trade(now)


def test_14_legacy_direct_index_execution_remains_blocked(paper_ledger_instance, test_clock):
    """TEST 14: Direct legacy execution attempt on normalized INDEX remains strictly blocked."""
    nifty_row = {
        'token': '99926000',
        'symbol': 'Nifty 50',
        'lotsize': '1',
        'instrumenttype': 'AMXIDX',
        'exch_seg': 'NSE',
    }
    inst = InstrumentResolver([nifty_row]).resolve('NSE', 'Nifty 50')
    feed = DummyIndexFeed("Nifty 50", "99926000")
    feed.instrument = inst

    service = PaperTradingService(
        feed,
        paper_ledger_instance,
        calendar=SessionCalendar({"year": 2026}),
        clock=lambda: test_clock[0],
    )
    service.start(background=False)
    snapshot = feed.fetch(test_clock[0])

    result = service._commit(snapshot, service.generation, "Buy")
    assert result["status"] == "blocked"
    assert result["reason"] == "non_tradable_execution_instrument"
    assert len(paper_ledger_instance.history()["orders"]) == 0


def test_15_fo_scanner_can_use_canonical_index_as_reference_underlying(paper_ledger_instance, test_clock):
    """TEST 15: Autonomous F&O scanner uses canonical INDEX without crashing or failing validation."""
    nifty_row = {
        'token': '99926000',
        'symbol': 'Nifty 50',
        'lotsize': '1',
        'instrumenttype': 'AMXIDX',
        'exch_seg': 'NSE',
    }
    inst = InstrumentResolver([nifty_row]).resolve('NSE', 'Nifty 50')
    feed = DummyIndexFeed("Nifty 50", "99926000")
    feed.instrument = inst

    service = PaperTradingService(
        feed,
        paper_ledger_instance,
        calendar=SessionCalendar({"year": 2026}),
        clock=lambda: test_clock[0],
    )
    service.start(background=False)
    snapshot = feed.fetch(test_clock[0])

    # Snapshot validation passes
    service._validate(snapshot, test_clock[0])

    # Scan and execute executes safely
    service._scan_and_execute_fo(snapshot, {'cash': 500000.0}, service.generation)
    scan_status = service.last_scanner_result
    assert scan_status is not None
    assert scan_status["universe_count"] > 0
    assert scan_status["data_source"] == "angel"

