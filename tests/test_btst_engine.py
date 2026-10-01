"""
Unit tests for Dedicated BTST (Buy Today, Sell Tomorrow) Strategy Engine.
Validates time windows, closing strength scoring, derivatives build-up,
overnight event risk filtering, anti-chase guardrails, and mandatory SL/Target invariants.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from tradingagents.integrations.angel_one.models import MarketBias
from tradingagents.runtime.btst_engine import (
    BTSTSetupState,
    BTSTStrategyEngine,
    DerivativesBuildUp,
    TradeType,
)

IST = ZoneInfo("Asia/Kolkata")


def test_btst_time_window_validation():
    """Verify BTST engine enforces strict 14:45 - 15:15 IST execution window."""
    engine = BTSTStrategyEngine()

    # 1. Before window (11:30 IST)
    dt_early = datetime(2026, 9, 28, 11, 30, tzinfo=IST)
    is_active, reason = engine.is_in_btst_window(dt_early)
    assert not is_active
    assert "BTST_TOO_EARLY" in reason

    # 2. Inside window (14:55 IST)
    dt_active = datetime(2026, 9, 28, 14, 55, tzinfo=IST)
    is_active, reason = engine.is_in_btst_window(dt_active)
    assert is_active
    assert reason == "BTST_WINDOW_ACTIVE"

    # 3. Exactly at cutoff (15:15 IST)
    dt_cutoff = datetime(2026, 9, 28, 15, 15, tzinfo=IST)
    is_active, reason = engine.is_in_btst_window(dt_cutoff)
    assert not is_active
    assert reason == "BTST_ENTRY_WINDOW_CLOSED"

    # 4. After cutoff (15:20 IST)
    dt_late = datetime(2026, 9, 28, 15, 20, tzinfo=IST)
    is_active, reason = engine.is_in_btst_window(dt_late)
    assert not is_active
    assert reason == "BTST_ENTRY_WINDOW_CLOSED"


def test_derivatives_buildup_classification():
    """Verify futures and options OI buildup classification."""
    engine = BTSTStrategyEngine()

    # Price Up, OI Up -> Long Buildup
    assert engine.evaluate_derivatives_buildup(0.8, 3.2) == DerivativesBuildUp.LONG_BUILDUP

    # Price Down, OI Up -> Short Buildup
    assert engine.evaluate_derivatives_buildup(-0.7, 4.0) == DerivativesBuildUp.SHORT_BUILDUP

    # Price Up, OI Down -> Short Covering
    assert engine.evaluate_derivatives_buildup(0.9, -2.5) == DerivativesBuildUp.SHORT_COVERING

    # Price Down, OI Down -> Long Unwinding
    assert engine.evaluate_derivatives_buildup(-0.8, -3.0) == DerivativesBuildUp.LONG_UNWINDING

    # Low OI change -> Neutral
    assert engine.evaluate_derivatives_buildup(0.5, 0.4) == DerivativesBuildUp.NEUTRAL


def test_overnight_event_risk_filter():
    """Verify event risk filter rejects binary overnight volatility events."""
    engine = BTSTStrategyEngine()

    # 1. Clean day with normal IV
    clean, msg = engine.check_overnight_event_risk("RELIANCE", date(2026, 9, 28), iv=0.22)
    assert clean
    assert msg == "CLEAN_OVERNIGHT_CALENDAR"

    # 2. Extreme IV (> 50%) -> Volatility crush risk
    clean_iv, msg_iv = engine.check_overnight_event_risk("RELIANCE", date(2026, 9, 28), iv=0.65)
    assert not clean_iv
    assert "Extreme IV" in msg_iv

    # 3. Scheduled earnings / major corporate event
    event_date = date(2026, 10, 15)
    engine.known_high_risk_events["INFY"] = [event_date]

    clean_ev, msg_ev = engine.check_overnight_event_risk("INFY", event_date, iv=0.20)
    assert not clean_ev
    assert "High-impact corporate event" in msg_ev


def test_bullish_btst_candidate_generation():
    """Verify bullish BTST candidate generation with mandatory SL/Target invariants."""
    engine = BTSTStrategyEngine()

    # Construct 35 bars: consolidation for 25 bars then clean accumulation into close
    n = 35
    start_dt = datetime(2026, 9, 28, 9, 15)
    opens, highs, lows, closes, vols = [], [], [], [], []

    for i in range(25):
        opens.append(1000.0)
        highs.append(1004.0)
        lows.append(996.0)
        closes.append(1000.0 + (i % 2))
        vols.append(5000)

    for i in range(10):
        p = 1002.0 + (i * 1.0)
        opens.append(p - 1.0)
        highs.append(p + 2.0)
        lows.append(p - 1.0)
        closes.append(p + 1.5)
        vols.append(8000 + i * 1000)

    df = pd.DataFrame({
        "Timestamp": [start_dt + timedelta(minutes=i * 5) for i in range(n)],
        "Open": opens,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": vols,
    })

    eval_time = datetime(2026, 9, 28, 14, 50, tzinfo=IST)
    result = engine.evaluate_btst_candidate(
        underlying="RELIANCE",
        df=df,
        eval_time=eval_time,
        futures_oi_change_pct=3.5,
        iv=0.20,
    )

    assert result.state == BTSTSetupState.BTST_TRIGGERED
    assert result.candidate is not None

    cand = result.candidate
    assert cand.trade_type == TradeType.BTST
    assert cand.strategy_id in ("BTST_BREAKOUT", "BTST_TREND_CONTINUATION")
    assert cand.action == "CE_BUY"
    assert cand.bias == MarketBias.BULLISH

    # Mandatory Invariants: SL < Entry < Target and R:R >= 1.5
    assert cand.stop_loss < cand.entry_price < cand.target
    assert cand.risk_reward_ratio >= 1.5
    assert cand.buildup == DerivativesBuildUp.LONG_BUILDUP


def test_anti_chase_overextended_rejection():
    """Verify BTST engine rejects parabolic moves extended > 2.5 ATR from VWAP."""
    engine = BTSTStrategyEngine()

    # Construct candles where the final bar spikes unrealistically high
    n = 35
    start_dt = datetime(2026, 9, 28, 9, 15)
    data = {
        "Timestamp": [start_dt + timedelta(minutes=i * 5) for i in range(n)],
        "Open": [1000.0] * n,
        "High": [1002.0] * (n - 1) + [1150.0],
        "Low": [998.0] * (n - 1) + [1000.0],
        "Close": [1000.0] * (n - 1) + [1145.0],
        "Volume": [10000] * n,
    }
    df = pd.DataFrame(data)

    eval_time = datetime(2026, 9, 28, 14, 50, tzinfo=IST)
    result = engine.evaluate_btst_candidate(
        underlying="SBIN",
        df=df,
        eval_time=eval_time,
        futures_oi_change_pct=1.0,
        iv=0.20,
    )

    assert result.state == BTSTSetupState.BTST_TOO_LATE
    assert result.candidate is None
    assert "BTST_TOO_LATE" in result.reason
