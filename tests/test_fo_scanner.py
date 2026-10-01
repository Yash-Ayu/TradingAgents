"""
Unit tests for Autonomous Indian F&O Universe Scanner and Early-Setup Detection Engine.
Validates dynamic universe discovery, early setup recognition (ARMED/TRIGGERED),
volatility squeeze, anti-chase guardrails, and mandatory SL/Target invariants.
"""

from datetime import datetime, timedelta

import pandas as pd

from tradingagents.integrations.angel_one.models import DerivativeType, MarketBias
from tradingagents.runtime.fo_scanner import (
    FOScanner,
    SetupState,
)


class MockScripMaster:
    """Mock scrip master for deterministic universe discovery testing."""
    def get_fo_universe(self, exchange: str = "NFO"):
        return {
            "NIFTY": {"symbol": "NIFTY", "is_index": True, "lot_size": 25},
            "BANKNIFTY": {"symbol": "BANKNIFTY", "is_index": True, "lot_size": 15},
            "RELIANCE": {"symbol": "RELIANCE", "is_index": False, "lot_size": 250},
            "HDFCBANK": {"symbol": "HDFCBANK", "is_index": False, "lot_size": 550},
            "ABCAPITAL": {"symbol": "ABCAPITAL", "is_index": False, "lot_size": 5400},
        }


def test_fo_universe_discovery_prioritization():
    """Verify indices and liquid stocks are prioritized first in universe discovery."""
    scanner = FOScanner(scrip_master=MockScripMaster())
    ordered = scanner.discover_universe()

    # Indices first
    assert ordered[0] in ("BANKNIFTY", "NIFTY")
    assert ordered[1] in ("BANKNIFTY", "NIFTY")

    # High-liquidity stocks next
    assert ordered[2] in ("HDFCBANK", "RELIANCE")
    assert ordered[3] in ("HDFCBANK", "RELIANCE")

    # Remaining stocks last
    assert ordered[4] == "ABCAPITAL"


def test_insufficient_data_rejection():
    """Verify scanner rejects datasets with fewer than 25 bars (fail-closed)."""
    scanner = FOScanner(scrip_master=MockScripMaster())

    # Only 10 bars
    data = {
        "Open": [100.0] * 10,
        "High": [102.0] * 10,
        "Low": [98.0] * 10,
        "Close": [100.0] * 10,
        "Volume": [1000] * 10,
    }
    df = pd.DataFrame(data)

    res = scanner.evaluate_price_action("NIFTY", df)
    assert res.state == SetupState.NO_SETUP
    assert res.candidate is None
    assert "Insufficient bar history" in res.reason


def test_hammer_rejection_trigger_and_mandatory_invariants():
    """Verify bullish hammer candlestick pattern triggers CE_BUY with valid SL & Target."""
    scanner = FOScanner(scrip_master=MockScripMaster())

    # Construct 30 bars of stable consolidation around 24000
    n = 30
    start_dt = datetime(2026, 9, 28, 9, 15)
    closes = [24000.0 + ((i % 3) - 1) * 5.0 for i in range(n - 1)] + [24005.0]
    opens = [24000.0] * (n - 1) + [24000.0]
    highs = [24040.0] * (n - 1) + [24010.0]
    lows = [23960.0] * (n - 1) + [23910.0]
    vols = [5000] * (n - 1) + [15000]

    df = pd.DataFrame({
        "Timestamp": [start_dt + timedelta(minutes=i * 5) for i in range(n)],
        "Open": opens,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": vols,
    })

    res = scanner.evaluate_price_action("NIFTY", df, is_index=True)

    assert res.state == SetupState.TRIGGERED
    assert res.bias == MarketBias.BULLISH
    assert res.candidate is not None

    cand = res.candidate
    assert cand.action == "CE_BUY"
    assert cand.is_index is True
    assert cand.instrument_type == DerivativeType.OPTIDX

    # Mandatory Invariant: SL < Entry < Target and R:R >= 1.5
    assert cand.stop_loss < cand.entry_price < cand.target
    assert cand.risk_reward_ratio >= 1.5


def test_anti_chase_too_late_rejection():
    """Verify scanner rejects moves extending too far above VWAP as TOO_LATE."""
    scanner = FOScanner(scrip_master=MockScripMaster(), max_chase_atr_multiplier=0.8)

    n = 30
    start_dt = datetime(2026, 9, 28, 9, 15)
    closes_late = [1000.0 + ((i % 3) - 1) * 2.0 for i in range(28)]
    opens_late = [1000.0] * 28
    highs_late = [1003.0] * 28
    lows_late = [997.0] * 28
    vols_late = [5000] * 28

    # Bar 28 inside bar
    opens_late.append(1000.0)
    highs_late.append(1001.0)
    lows_late.append(999.0)
    closes_late.append(1000.0)
    vols_late.append(5000)

    # Bar 29 breakout: cmp = 1006.0, prev_high = 1001.0 (extended > 0.8x ATR above VWAP)
    opens_late.append(1000.0)
    highs_late.append(1007.0)
    lows_late.append(999.5)
    closes_late.append(1006.0)
    vols_late.append(15000)

    df_late = pd.DataFrame({
        "Timestamp": [start_dt + timedelta(minutes=i * 5) for i in range(n)],
        "Open": opens_late,
        "High": highs_late,
        "Low": lows_late,
        "Close": closes_late,
        "Volume": vols_late,
    })

    res = scanner.evaluate_price_action("RELIANCE", df_late, is_index=False)
    assert res.state == SetupState.TOO_LATE
    assert res.candidate is None
    assert "TOO_LATE" in res.reason
