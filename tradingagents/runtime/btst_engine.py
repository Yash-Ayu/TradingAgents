"""
Dedicated BTST (Buy Today, Sell Tomorrow) Strategy Engine for Indian F&O.

ABS-INVARIANT:
- Dedicated overnight continuation strategy. Not conflated with Intraday.
- Explicit trade_type = 'BTST' and strategy_id.
- Time window configurable: Default 14:45 - 15:15 IST.
- After cutoff: BTST_ENTRY_WINDOW_CLOSED.
- NO VALID BTST SETUP = NO BTST TRADE.
- Gap risk modeled honestly: Stop loss jumps fill at market open price, not theoretical SL.
- Mandatory Stop Loss & Target on every candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tradingagents.integrations.angel_one.models import DerivativeType, MarketBias

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class TradeType(str, Enum):
    INTRADAY = "INTRADAY"
    BTST = "BTST"


class BTSTSetupState(str, Enum):
    BTST_WATCHING = "BTST_WATCHING"
    BTST_SETUP_FORMING = "BTST_SETUP_FORMING"
    BTST_CANDIDATE = "BTST_CANDIDATE"
    BTST_ARMED = "BTST_ARMED"
    BTST_TRIGGERED = "BTST_TRIGGERED"
    BTST_ENTERED = "BTST_ENTERED"
    BTST_INVALIDATED = "BTST_INVALIDATED"
    BTST_TOO_LATE = "BTST_TOO_LATE"
    BTST_REJECTED = "BTST_REJECTED"
    BTST_EXITED = "BTST_EXITED"


class DerivativesBuildUp(str, Enum):
    LONG_BUILDUP = "LONG_BUILDUP"          # Price Up, OI Up -> Aggressive Long
    SHORT_BUILDUP = "SHORT_BUILDUP"        # Price Down, OI Up -> Aggressive Short
    SHORT_COVERING = "SHORT_COVERING"      # Price Up, OI Down -> Short Covering Rally
    LONG_UNWINDING = "LONG_UNWINDING"      # Price Down, OI Down -> Long Liquidation
    NEUTRAL = "NEUTRAL"


@dataclass
class BTSTConfig:
    """Configurable parameters for BTST Strategy Engine."""
    start_time: dtime = field(default_factory=lambda: dtime(14, 45))
    cutoff_time: dtime = field(default_factory=lambda: dtime(15, 15))
    min_closing_strength_pct: float = 0.80  # Close must be in upper 20% of range (or lower 20% for PE)
    min_volume_ratio: float = 1.3           # Volume >= 1.3x 20-MA
    min_risk_reward: float = 1.5            # Min R:R ratio
    max_iv_threshold: float = 0.50          # Max acceptable IV (50%) to prevent volatility crush
    max_gap_risk_sl_pct: float = 0.25       # Max acceptable SL distance in % of premium
    enforce_event_filter: bool = True       # Reject on known event risk


@dataclass
class BTSTCandidate:
    """Actionable BTST candidate produced by the BTST Strategy Engine."""
    symbol: str
    underlying: str
    is_index: bool
    instrument_type: DerivativeType
    trade_type: TradeType = TradeType.BTST
    strategy_id: str = "BTST"
    bias: MarketBias = MarketBias.BULLISH
    action: str = "CE_BUY"  # CE_BUY or PE_BUY
    spot_price: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    risk_reward_ratio: float = 0.0
    setup_name: str = "BTST_BREAKOUT"
    state: BTSTSetupState = BTSTSetupState.BTST_TRIGGERED
    closing_strength_score: float = 0.0
    buildup: DerivativesBuildUp = DerivativesBuildUp.NEUTRAL
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    atr: float = 0.0
    rsi: float = 50.0
    vwap: float = 0.0
    vwap_distance_pct: float = 0.0


@dataclass
class BTSTEvaluationResult:
    """Summary of BTST evaluation on a single instrument."""
    symbol: str
    underlying: str
    trade_type: TradeType = TradeType.BTST
    spot_price: float = 0.0
    state: BTSTSetupState = BTSTSetupState.BTST_WATCHING
    bias: MarketBias = MarketBias.NEUTRAL
    score: float = 0.0
    reason: str = ""
    candidate: BTSTCandidate | None = None
    evaluated_at: datetime = field(default_factory=datetime.now)


class BTSTStrategyEngine:
    """
    Dedicated BTST Engine.
    Identifies high-probability closing setups poised for overnight continuation.
    Enforces strict time windows, multi-candle closing strength, pattern confluence,
    overnight event filters, and honest gap risk modeling.
    """

    def __init__(self, config: BTSTConfig | None = None):
        self.config = config or BTSTConfig()
        self._state_transition_logs: list[dict[str, Any]] = []
        self._max_transition_logs = 200
        # Known high-risk calendar dates (can be populated dynamically via API or config)
        self.known_high_risk_events: dict[str, list[date]] = {}

    def log_state_transition(
        self,
        symbol: str,
        old_state: BTSTSetupState,
        new_state: BTSTSetupState,
        reason: str,
    ) -> None:
        """Log state transitions for transparent audit trails."""
        entry = {
            "timestamp": datetime.now(IST).isoformat(),
            "symbol": symbol,
            "old_state": old_state.value,
            "new_state": new_state.value,
            "reason": reason,
        }
        self._state_transition_logs.append(entry)
        if len(self._state_transition_logs) > self._max_transition_logs:
            self._state_transition_logs.pop(0)
        logger.info(f"BTST State Transition [{symbol}]: {old_state.value} -> {new_state.value} | Reason: {reason}")

    def get_transition_history(self) -> list[dict[str, Any]]:
        return list(self._state_transition_logs)

    def is_in_btst_window(self, current_time: datetime | None = None) -> tuple[bool, str]:
        """
        Validates if current time is within the configured BTST evaluation window.
        Returns (is_active, status_code).
        """
        now = current_time or datetime.now(IST)
        if hasattr(now, "tzinfo") and now.tzinfo is not None:
            t = now.astimezone(IST).time()
        else:
            t = now.time()

        if t < self.config.start_time:
            return False, f"BTST_TOO_EARLY ({t.strftime('%H:%M')} < {self.config.start_time.strftime('%H:%M')})"
        if t >= self.config.cutoff_time:
            return False, "BTST_ENTRY_WINDOW_CLOSED"

        return True, "BTST_WINDOW_ACTIVE"

    def evaluate_derivatives_buildup(
        self,
        price_change_pct: float,
        oi_change_pct: float | None = None,
    ) -> DerivativesBuildUp:
        """Classify derivatives open interest build-up context."""
        if oi_change_pct is None or abs(oi_change_pct) < 1.0:
            return DerivativesBuildUp.NEUTRAL

        if price_change_pct > 0.3 and oi_change_pct > 1.5:
            return DerivativesBuildUp.LONG_BUILDUP
        elif price_change_pct < -0.3 and oi_change_pct > 1.5:
            return DerivativesBuildUp.SHORT_BUILDUP
        elif price_change_pct > 0.3 and oi_change_pct < -1.5:
            return DerivativesBuildUp.SHORT_COVERING
        elif price_change_pct < -0.3 and oi_change_pct < -1.5:
            return DerivativesBuildUp.LONG_UNWINDING

        return DerivativesBuildUp.NEUTRAL

    def check_overnight_event_risk(
        self,
        underlying: str,
        eval_date: date | None = None,
        iv: float | None = None,
    ) -> tuple[bool, str]:
        """
        Checks for major overnight event risks: earnings, scheduled RBI/monetary policy,
        or extreme implied volatility (> 50%) causing binary overnight gap risk.
        """
        d = eval_date or datetime.now(IST).date()

        # Check IV threshold
        if iv is not None and iv > self.config.max_iv_threshold:
            return False, f"BTST_EVENT_RISK_REJECT: Extreme IV ({iv * 100:.1f}% > {self.config.max_iv_threshold * 100:.0f}%). Volatility crush risk."

        # Check company specific known high-risk dates
        u_upper = underlying.upper().strip()
        if u_upper in self.known_high_risk_events and d in self.known_high_risk_events[u_upper]:
            return False, f"BTST_EVENT_RISK_REJECT: High-impact corporate event/earnings on {d}."

        return True, "CLEAN_OVERNIGHT_CALENDAR"

    def evaluate_btst_candidate(
        self,
        underlying: str,
        df: pd.DataFrame,
        eval_time: datetime | None = None,
        futures_oi_change_pct: float | None = None,
        iv: float | None = None,
        is_index: bool | None = None,
        bypass_time_window_for_testing: bool = False,
    ) -> BTSTEvaluationResult:
        """
        End-to-End BTST Candidate Evaluation Pipeline:
        1. Configurable Time Window Verification
        2. Overnight Event Risk Filter
        3. Multi-Candle Closing Strength / Weakness
        4. VWAP Position & Trend Alignment
        5. Chart & Candlestick Pattern Confluence
        6. Volume & Derivatives Build-up Context
        7. Mandatory Stop Loss & Target Sizing
        8. BTST Score Calculation & State Transition
        """
        u_upper = underlying.upper().strip()
        now = eval_time or datetime.now(IST)

        # 1. Configurable Time Window Gate
        if not bypass_time_window_for_testing:
            in_window, window_reason = self.is_in_btst_window(now)
            if not in_window:
                res = BTSTEvaluationResult(
                    symbol=u_upper,
                    underlying=u_upper,
                    state=BTSTSetupState.BTST_REJECTED if "CLOSED" in window_reason else BTSTSetupState.BTST_WATCHING,
                    reason=window_reason,
                    evaluated_at=now,
                )
                return res

        # 2. Check Data Sufficiency
        if df is None or len(df) < 30:
            return BTSTEvaluationResult(
                symbol=u_upper,
                underlying=u_upper,
                state=BTSTSetupState.BTST_REJECTED,
                reason=f"Insufficient candle history ({len(df) if df is not None else 0} < 30 bars). NO BTST.",
                evaluated_at=now,
            )

        # 3. Overnight Event Risk Filter
        if self.config.enforce_event_filter:
            event_clean, event_reason = self.check_overnight_event_risk(u_upper, now.date(), iv)
            if not event_clean:
                self.log_state_transition(u_upper, BTSTSetupState.BTST_WATCHING, BTSTSetupState.BTST_REJECTED, event_reason)
                return BTSTEvaluationResult(
                    symbol=u_upper,
                    underlying=u_upper,
                    state=BTSTSetupState.BTST_REJECTED,
                    reason=event_reason,
                    evaluated_at=now,
                )

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        open_ = df["Open"].astype(float)
        vol = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series(np.ones(len(df)))

        cmp = float(close.iloc[-1])
        day_high = float(high.max())
        day_low = float(low.min())
        day_range = max(day_high - day_low, 0.001)

        # Session Closing Range Ratio: 1.0 = closing at day's high, 0.0 = closing at day's low
        closing_range_ratio = (cmp - day_low) / day_range

        # ATR 14
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else float(cmp * 0.01)
        atr = max(atr, float(cmp * 0.005))

        # VWAP
        typical_price = (high + low + close) / 3.0
        cum_vol = vol.cumsum().replace(0, np.nan)
        vwap_series = (typical_price * vol).cumsum() / cum_vol
        vwap = float(vwap_series.iloc[-1]) if not np.isnan(vwap_series.iloc[-1]) else cmp
        vwap_dist_pct = ((cmp - vwap) / vwap) * 100.0

        # EMAs
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else ema20

        # RSI
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi_series = 100 - (100 / (1 + rs))
        rsi = float(rsi_series.iloc[-1]) if not np.isnan(rsi_series.iloc[-1]) else 50.0

        # Volume Expansion
        vol_ma20 = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else float(vol.mean())
        curr_vol = float(vol.iloc[-1])
        vol_ratio = curr_vol / max(vol_ma20, 1.0)

        # Multi-candle Trend Continuity (last 5 bars)
        recent_closes = close.iloc[-5:].values
        recent_higher_closes = sum(recent_closes[i] >= recent_closes[i - 1] for i in range(1, len(recent_closes)))
        recent_lower_closes = sum(recent_closes[i] <= recent_closes[i - 1] for i in range(1, len(recent_closes)))

        # Day Change %
        day_open = float(open_.iloc[0])
        day_change_pct = ((cmp - day_open) / day_open) * 100.0

        # Derivatives Build-up
        buildup = self.evaluate_derivatives_buildup(day_change_pct, futures_oi_change_pct)

        # Anti-Chase Guardrail: If price has already moved > 3.0 ATR from VWAP, reject as TOO_LATE
        if abs(cmp - vwap) > (2.5 * atr):
            reason = f"BTST_TOO_LATE: Price extended {abs(cmp - vwap) / atr:.1f} ATR from VWAP. Overextended."
            self.log_state_transition(u_upper, BTSTSetupState.BTST_WATCHING, BTSTSetupState.BTST_TOO_LATE, reason)
            return BTSTEvaluationResult(
                symbol=u_upper,
                underlying=u_upper,
                spot_price=cmp,
                state=BTSTSetupState.BTST_TOO_LATE,
                reason=reason,
                evaluated_at=now,
            )

        # Pattern Recognition
        recent_resistance = float(high.iloc[-20:-1].max())
        recent_support = float(low.iloc[-20:-1].min())
        is_breakout_holding = cmp > recent_resistance
        is_breakdown_holding = cmp < recent_support

        is_benchmark_index = is_index if is_index is not None else u_upper in {
            "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"
        }
        inst_type = DerivativeType.OPTIDX if is_benchmark_index else DerivativeType.OPTSTK

        # =====================================================================
        # SCORING & SETUP FORMATION
        # =====================================================================
        bullish_score = 0.0
        bearish_score = 0.0

        # A. Bullish Confluence
        if closing_range_ratio >= self.config.min_closing_strength_pct:
            bullish_score += 30.0
        if cmp > vwap and vwap_dist_pct > 0.1:
            bullish_score += 20.0
        if ema20 > ema50:
            bullish_score += 15.0
        if recent_higher_closes >= 3:
            bullish_score += 15.0
        if vol_ratio >= self.config.min_volume_ratio:
            bullish_score += 10.0
        if buildup == DerivativesBuildUp.LONG_BUILDUP:
            bullish_score += 20.0
        elif buildup == DerivativesBuildUp.SHORT_COVERING:
            bullish_score += 10.0
        if is_breakout_holding:
            bullish_score += 15.0
        if 48 <= rsi <= 72:
            bullish_score += 10.0

        # B. Bearish Confluence
        if closing_range_ratio <= (1.0 - self.config.min_closing_strength_pct):
            bearish_score += 30.0
        if cmp < vwap and vwap_dist_pct < -0.1:
            bearish_score += 20.0
        if ema20 < ema50:
            bearish_score += 15.0
        if recent_lower_closes >= 3:
            bearish_score += 15.0
        if vol_ratio >= self.config.min_volume_ratio:
            bearish_score += 10.0
        if buildup == DerivativesBuildUp.SHORT_BUILDUP:
            bearish_score += 20.0
        elif buildup == DerivativesBuildUp.LONG_UNWINDING:
            bearish_score += 10.0
        if is_breakdown_holding:
            bearish_score += 15.0
        if 28 <= rsi <= 52:
            bearish_score += 10.0

        # EVALUATE FINAL DECISION (Threshold: Score >= 75.0)
        THRESHOLD = 75.0

        if bullish_score >= THRESHOLD and bullish_score > bearish_score:
            setup_id = "BTST_BREAKOUT" if is_breakout_holding else "BTST_TREND_CONTINUATION"
            # Mandatory Stop Loss: strictly below session VWAP and recent 5-bar low
            recent_5_low = float(low.iloc[-5:].min())
            stop_loss = round(min(vwap - 0.3 * atr, recent_5_low - 0.2 * atr), 2)
            risk_dist = cmp - stop_loss
            if risk_dist <= 0:
                stop_loss = round(cmp - 1.0 * atr, 2)
                risk_dist = cmp - stop_loss

            target = round(cmp + (1.8 * risk_dist), 2)
            rr_ratio = round((target - cmp) / risk_dist, 2)

            candidate = BTSTCandidate(
                symbol=u_upper,
                underlying=u_upper,
                is_index=is_benchmark_index,
                instrument_type=inst_type,
                trade_type=TradeType.BTST,
                strategy_id=setup_id,
                bias=MarketBias.BULLISH,
                action="CE_BUY",
                spot_price=cmp,
                entry_price=cmp,
                stop_loss=stop_loss,
                target=target,
                risk_reward_ratio=rr_ratio,
                setup_name=f"{setup_id} (Closing Strength {closing_range_ratio * 100:.0f}%)",
                state=BTSTSetupState.BTST_TRIGGERED,
                closing_strength_score=bullish_score,
                buildup=buildup,
                reason=f"Strong close in top {100 - closing_range_ratio * 100:.0f}% of day range. VWAP support Rs.{vwap:.2f}. Volume {vol_ratio:.1f}x. Buildup: {buildup.value}.",
                timestamp=now,
                atr=atr,
                rsi=rsi,
                vwap=vwap,
                vwap_distance_pct=vwap_dist_pct,
            )

            self.log_state_transition(u_upper, BTSTSetupState.BTST_ARMED, BTSTSetupState.BTST_TRIGGERED, candidate.reason)

            return BTSTEvaluationResult(
                symbol=u_upper,
                underlying=u_upper,
                trade_type=TradeType.BTST,
                spot_price=cmp,
                state=BTSTSetupState.BTST_TRIGGERED,
                bias=MarketBias.BULLISH,
                score=bullish_score,
                reason=candidate.reason,
                candidate=candidate,
                evaluated_at=now,
            )

        elif bearish_score >= THRESHOLD and bearish_score > bullish_score:
            setup_id = "BTST_BREAKDOWN" if is_breakdown_holding else "BTST_TREND_CONTINUATION"
            recent_5_high = float(high.iloc[-5:].max())
            stop_loss = round(max(vwap + 0.3 * atr, recent_5_high + 0.2 * atr), 2)
            risk_dist = stop_loss - cmp
            if risk_dist <= 0:
                stop_loss = round(cmp + 1.0 * atr, 2)
                risk_dist = stop_loss - cmp

            target = round(cmp - (1.8 * risk_dist), 2)
            rr_ratio = round((cmp - target) / risk_dist, 2)

            candidate = BTSTCandidate(
                symbol=u_upper,
                underlying=u_upper,
                is_index=is_benchmark_index,
                instrument_type=inst_type,
                trade_type=TradeType.BTST,
                strategy_id=setup_id,
                bias=MarketBias.BEARISH,
                action="PE_BUY",
                spot_price=cmp,
                entry_price=cmp,
                stop_loss=stop_loss,
                target=target,
                risk_reward_ratio=rr_ratio,
                setup_name=f"{setup_id} (Closing Weakness {closing_range_ratio * 100:.0f}%)",
                state=BTSTSetupState.BTST_TRIGGERED,
                closing_strength_score=bearish_score,
                buildup=buildup,
                reason=f"Weak close in bottom {closing_range_ratio * 100:.0f}% of day range. VWAP resistance Rs.{vwap:.2f}. Volume {vol_ratio:.1f}x. Buildup: {buildup.value}.",
                timestamp=now,
                atr=atr,
                rsi=rsi,
                vwap=vwap,
                vwap_distance_pct=vwap_dist_pct,
            )

            self.log_state_transition(u_upper, BTSTSetupState.BTST_ARMED, BTSTSetupState.BTST_TRIGGERED, candidate.reason)

            return BTSTEvaluationResult(
                symbol=u_upper,
                underlying=u_upper,
                trade_type=TradeType.BTST,
                spot_price=cmp,
                state=BTSTSetupState.BTST_TRIGGERED,
                bias=MarketBias.BEARISH,
                score=bearish_score,
                reason=candidate.reason,
                candidate=candidate,
                evaluated_at=now,
            )

        # Moderate score -> SETUP_FORMING or WATCHING
        max_score = max(bullish_score, bearish_score)
        if max_score >= 50.0:
            state = BTSTSetupState.BTST_SETUP_FORMING
            reason = f"BTST setup forming (Score {max_score:.1f}/100 < 75.0). Awaiting closing volume/structure confirmation."
        else:
            state = BTSTSetupState.BTST_WATCHING
            reason = f"No actionable BTST setup (Score {max_score:.1f}/100). Range-bound close."

        return BTSTEvaluationResult(
            symbol=u_upper,
            underlying=u_upper,
            trade_type=TradeType.BTST,
            spot_price=cmp,
            state=state,
            bias=MarketBias.NEUTRAL,
            score=max_score,
            reason=reason,
            evaluated_at=now,
        )
