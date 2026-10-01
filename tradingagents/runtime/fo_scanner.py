"""
Autonomous Indian F&O Universe Scanner and Early-Setup Detection Engine.

Scans the dynamic Angel One F&O universe (Indices + 210 Stock Options/Futures) to detect
high-probability setups BEFORE major moves happen.

Key Principles:
1. NO MOMENTUM CHASING: Rejects already extended moves (TOO_LATE / OVEREXTENDED).
2. MULTI-TIMEFRAME & SMART-MONEY AWARE: Analyzes VWAP, Order Blocks, Volume Spikes, Squeeze Contraction.
3. STAGED STATE MACHINE: SETUP_FORMING -> ARMED -> TRIGGERED -> ENTRY.
4. HONEST DATA: Zero fake/synthetic data labeled as live.
5. FAIL-CLOSED: NO VALID SETUP = NO TRADE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from tradingagents.integrations.angel_one.models import DerivativeType, MarketBias
from tradingagents.integrations.angel_one.scrip_master import ScripMasterManager

logger = logging.getLogger(__name__)

# Default benchmark indices
BENCHMARK_INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

# Priority liquid stocks for fast-tier scanning
HIGH_LIQUIDITY_F_AND_O_STOCKS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS",
    "AXISBANK", "TATAMOTORS", "TATASTEEL", "BAJFINANCE", "KOTAKBANK",
    "LT", "MARUTI", "BHARTIARTL", "ITC", "SUNPHARMA", "M&M", "HINDALCO"
]


class SetupState(str, Enum):
    NO_SETUP = "NO_SETUP"
    SETUP_FORMING = "SETUP_FORMING"  # Squeeze / consolidation near key level
    ARMED = "ARMED"                  # At key support/resistance/VWAP with volume compression
    TRIGGERED = "TRIGGERED"          # Rejection wick or breakout confirmed with volume spike
    TOO_LATE = "TOO_LATE"            # Move already ran > 1.5 ATR (Do not chase)
    OVEREXTENDED = "OVEREXTENDED"    # Extreme RSI (>75 or <25) without reversal structure
    POOR_RR = "POOR_RR"              # Risk-to-Reward ratio < 1.5 : 1


@dataclass
class ScanCandidate:
    """Actionable F&O trade candidate produced by the scanner."""
    symbol: str
    underlying: str
    is_index: bool
    instrument_type: DerivativeType
    bias: MarketBias
    action: str  # CE_BUY or PE_BUY
    spot_price: float
    entry_price: float
    stop_loss: float
    target: float
    risk_reward_ratio: float
    setup_name: str
    state: SetupState
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)
    atr: float = 0.0
    rsi: float = 50.0
    vwap_distance_pct: float = 0.0


@dataclass
class ScanEvaluationResult:
    """Summary of an evaluation on a single ticker."""
    symbol: str
    underlying: str
    spot_price: float
    state: SetupState
    bias: MarketBias
    reason: str
    candidate: ScanCandidate | None = None
    evaluated_at: datetime = field(default_factory=datetime.now)


class FOScanner:
    """
    Autonomous Indian F&O Universe Scanner.
    Discovers tradable underlyings dynamically from Angel One Scrip Master
    and evaluates multi-timeframe price action and smart-money proxies.
    """

    def __init__(
        self,
        scrip_master: ScripMasterManager | None = None,
        min_risk_reward: float = 1.5,
        max_chase_atr_multiplier: float = 1.2,
    ):
        self.scrip_master = scrip_master or ScripMasterManager()
        self.min_risk_reward = min_risk_reward
        self.max_chase_atr_multiplier = max_chase_atr_multiplier
        self._universe_cache: dict[str, dict[str, Any]] | None = None
        self._last_audit_logs: list[dict[str, Any]] = []
        self._max_audit_logs = 100

    def discover_universe(self, exchange: str = "NFO", prioritize: bool = True) -> list[str]:
        """
        Dynamically discover available Indian F&O underlyings from Scrip Master.
        Prioritizes benchmark indices and high-liquidity stocks first.
        """
        universe_map = self.scrip_master.get_fo_universe(exchange=exchange)
        self._universe_cache = universe_map

        discovered_indices = []
        discovered_priority_stocks = []
        discovered_other_stocks = []

        for name, meta in universe_map.items():
            if meta["is_index"] or name in BENCHMARK_INDICES:
                discovered_indices.append(name)
            elif name in HIGH_LIQUIDITY_F_AND_O_STOCKS:
                discovered_priority_stocks.append(name)
            else:
                discovered_other_stocks.append(name)

        discovered_indices.sort()
        discovered_priority_stocks.sort()
        discovered_other_stocks.sort()

        if prioritize:
            ordered = discovered_indices + discovered_priority_stocks + discovered_other_stocks
        else:
            ordered = sorted(universe_map.keys())

        logger.info(
            f"F&O Universe discovered: {len(discovered_indices)} indices, "
            f"{len(discovered_priority_stocks)} priority stocks, "
            f"{len(discovered_other_stocks)} other stocks. Total: {len(ordered)}"
        )
        return ordered

    def evaluate_price_action(
        self,
        underlying: str,
        df: pd.DataFrame,
        is_index: bool | None = None,
    ) -> ScanEvaluationResult:
        """
        Evaluate OHLCV data for early setups, candlestick structures, and smart-money proxies.
        Enforces strict fail-closed and anti-chase rules.
        """
        u_upper = underlying.upper().strip()
        index_flag = is_index if is_index is not None else (u_upper in BENCHMARK_INDICES)

        if df is None or len(df) < 25:
            reason = f"Insufficient bar history ({len(df) if df is not None else 0} < 25 bars). NO TRADE."
            res = ScanEvaluationResult(
                symbol=u_upper,
                underlying=u_upper,
                spot_price=float(df['Close'].iloc[-1]) if df is not None and not df.empty else 0.0,
                state=SetupState.NO_SETUP,
                bias=MarketBias.NEUTRAL,
                reason=reason,
            )
            self._log_audit(res)
            return res

        close = df['Close'].astype(float)
        high = df['High'].astype(float)
        low = df['Low'].astype(float)
        open_ = df['Open'].astype(float)
        vol = df['Volume'].astype(float) if 'Volume' in df.columns else pd.Series(np.ones(len(df)))

        cmp = float(close.iloc[-1])
        cmp_open = float(open_.iloc[-1])
        cmp_high = float(high.iloc[-1])
        cmp_low = float(low.iloc[-1])
        prev_close = float(close.iloc[-2])
        prev_open = float(open_.iloc[-2])
        prev_high = float(high.iloc[-2])
        prev_low = float(low.iloc[-2])

        # 1. Moving Averages
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else ema20

        # 2. Session VWAP Proxy (Volume Weighted Average Price)
        # In intraday, cumsum(price * vol) / cumsum(vol)
        typical_price = (high + low + close) / 3.0
        cum_vol = vol.cumsum().replace(0, np.nan)
        cum_tp_vol = (typical_price * vol).cumsum()
        vwap_series = cum_tp_vol / cum_vol
        vwap = float(vwap_series.iloc[-1]) if not np.isnan(vwap_series.iloc[-1]) else ema20
        vwap_dist_pct = (cmp - vwap) / vwap * 100.0

        # 3. ATR 14
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else float(cmp * 0.01)
        if atr <= 0:
            atr = float(cmp * 0.01)

        # 4. RSI 14
        delta = close.diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
        with np.errstate(divide='ignore', invalid='ignore'):
            rs = np.where(loss == 0, np.where(gain > 0, np.inf, 1.0), gain / loss)
            rsi_series = np.where(np.isinf(rs), 100.0, 100.0 - (100.0 / (1.0 + rs)))
        rsi = float(rsi_series[-1]) if len(rsi_series) and not np.isnan(rsi_series[-1]) else 50.0

        # 5. Volatility Squeeze (Bollinger Bands vs Keltner Channels)
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_upper = sma20 + (2.0 * std20)
        bb_lower = sma20 - (2.0 * std20)
        kc_upper = sma20 + (1.5 * tr.rolling(20).mean())
        kc_lower = sma20 - (1.5 * tr.rolling(20).mean())
        bb_width = float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / sma20.iloc[-1]) if sma20.iloc[-1] else 0.0
        is_squeeze = bool(bb_upper.iloc[-1] <= kc_upper.iloc[-1] and bb_lower.iloc[-1] >= kc_lower.iloc[-1])

        # 6. Volume Spike Proxy
        vol_ma20 = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else float(vol.mean())
        curr_vol = float(vol.iloc[-1])
        vol_ratio = curr_vol / max(vol_ma20, 1.0)

        # 7. Candlestick Structure Analysis
        body = abs(cmp - cmp_open)
        upper_wick = cmp_high - max(cmp, cmp_open)
        lower_wick = min(cmp, cmp_open) - cmp_low
        total_range = cmp_high - cmp_low

        is_hammer_bullish = lower_wick >= (1.8 * max(body, 0.01 * atr)) and upper_wick <= (0.4 * total_range)
        is_shooting_star_bearish = upper_wick >= (1.8 * max(body, 0.01 * atr)) and lower_wick <= (0.4 * total_range)
        is_bullish_engulfing = (cmp > cmp_open) and (prev_close < prev_open) and (cmp >= prev_open) and (cmp_open <= prev_close)
        is_bearish_engulfing = (cmp < cmp_open) and (prev_close > prev_open) and (cmp <= prev_open) and (cmp_open >= prev_close)
        is_inside_bar_breakout_up = (prev_high <= float(high.iloc[-3])) and (prev_low >= float(low.iloc[-3])) and (cmp > prev_high)
        is_inside_bar_breakout_down = (prev_high <= float(high.iloc[-3])) and (prev_low >= float(low.iloc[-3])) and (cmp < prev_low)

        # 8. Order Block / Key S/R Zones (Recent 20-bar extremes)
        recent_support = float(low.iloc[-20:].min())
        recent_resistance = float(high.iloc[-20:].max())
        near_support = abs(cmp - recent_support) <= (0.8 * atr)
        near_resistance = abs(cmp - recent_resistance) <= (0.8 * atr)
        near_vwap = abs(cmp - vwap) <= (0.6 * atr)

        # =====================================================================
        # STATE & PATTERN EVALUATION
        # =====================================================================

        state = SetupState.NO_SETUP
        bias = MarketBias.NEUTRAL
        setup_name = "None"
        reason = "Consolidation / No actionable pre-move structure"
        stop_loss = cmp
        target = cmp

        # A. BULLISH SETUPS -> CE BUY
        if is_hammer_bullish and (near_support or near_vwap) and rsi < 65:
            state = SetupState.TRIGGERED
            bias = MarketBias.BULLISH
            setup_name = "Demand Rejection Hammer (VWAP/Support)"
            stop_loss = round(cmp_low - (0.2 * atr), 2)
            risk_dist = max(0.01, cmp - stop_loss)
            target = round(cmp + max(2.0 * atr, 1.8 * risk_dist), 2)
            reason = f"Bullish pin bar rejection at Rs.{cmp_low:.2f} near VWAP/Support. Volume {vol_ratio:.1f}x."

        elif is_bullish_engulfing and (near_support or near_vwap) and rsi < 65:
            state = SetupState.TRIGGERED
            bias = MarketBias.BULLISH
            setup_name = "Bullish Engulfing Base Reversal"
            stop_loss = round(min(cmp_low, prev_low) - (0.2 * atr), 2)
            risk_dist = max(0.01, cmp - stop_loss)
            target = round(cmp + max(2.0 * atr, 1.8 * risk_dist), 2)
            reason = f"Bullish engulfing candle over previous bar at Rs.{cmp:.2f}. Volume {vol_ratio:.1f}x."

        elif is_inside_bar_breakout_up and cmp > vwap and rsi < 68:
            state = SetupState.TRIGGERED
            bias = MarketBias.BULLISH
            setup_name = "Inside Bar Breakout (Bullish Continuation)"
            stop_loss = round(prev_low - (0.2 * atr), 2)
            risk_dist = max(0.01, cmp - stop_loss)
            target = round(cmp + max(2.2 * atr, 1.8 * risk_dist), 2)
            reason = f"Inside bar upward expansion above Rs.{prev_high:.2f} with VWAP support."

        elif is_squeeze and cmp > vwap and ema20 > ema50 and rsi < 65:
            state = SetupState.ARMED
            bias = MarketBias.BULLISH
            setup_name = "Bollinger Squeeze Pre-Breakout (Bullish Coil)"
            stop_loss = round(vwap - (1.0 * atr), 2)
            target = round(cmp + (2.5 * atr), 2)
            reason = f"Volatility contraction squeeze coiling above VWAP (Rs.{vwap:.2f}) with aligned EMAs."

        # B. BEARISH SETUPS -> PE BUY
        elif is_shooting_star_bearish and (near_resistance or near_vwap) and rsi > 35:
            state = SetupState.TRIGGERED
            bias = MarketBias.BEARISH
            setup_name = "Supply Rejection Shooting Star (VWAP/Resistance)"
            stop_loss = round(cmp_high + (0.2 * atr), 2)
            risk_dist = max(0.01, stop_loss - cmp)
            target = round(cmp - max(2.0 * atr, 1.8 * risk_dist), 2)
            reason = f"Bearish rejection wick at Rs.{cmp_high:.2f} near VWAP/Resistance. Volume {vol_ratio:.1f}x."

        elif is_bearish_engulfing and (near_resistance or near_vwap) and rsi > 35:
            state = SetupState.TRIGGERED
            bias = MarketBias.BEARISH
            setup_name = "Bearish Engulfing Breakdown"
            stop_loss = round(max(cmp_high, prev_high) + (0.2 * atr), 2)
            risk_dist = max(0.01, stop_loss - cmp)
            target = round(cmp - max(2.0 * atr, 1.8 * risk_dist), 2)
            reason = f"Bearish engulfing candle breaking below previous bar at Rs.{cmp:.2f}."

        elif is_inside_bar_breakout_down and cmp < vwap and rsi > 32:
            state = SetupState.TRIGGERED
            bias = MarketBias.BEARISH
            setup_name = "Inside Bar Breakdown (Bearish Continuation)"
            stop_loss = round(prev_high + (0.2 * atr), 2)
            risk_dist = max(0.01, stop_loss - cmp)
            target = round(cmp - max(2.2 * atr, 1.8 * risk_dist), 2)
            reason = f"Inside bar downward breakdown below Rs.{prev_low:.2f} under VWAP pressure."

        elif is_squeeze and cmp < vwap and ema20 < ema50 and rsi > 35:
            state = SetupState.ARMED
            bias = MarketBias.BEARISH
            setup_name = "Bollinger Squeeze Pre-Breakout (Bearish Coil)"
            stop_loss = round(vwap + (1.0 * atr), 2)
            target = round(cmp - (2.5 * atr), 2)
            reason = f"Volatility contraction squeeze coiling under VWAP (Rs.{vwap:.2f}) with bearish EMAs."

        # C. EARLY FORMING (Monitoring only)
        elif is_squeeze:
            state = SetupState.SETUP_FORMING
            bias = MarketBias.NEUTRAL
            setup_name = "Neutral Volatility Squeeze (Watch for trigger)"
            reason = f"Bollinger Bands tightly compressed inside Keltner Channel (width: {bb_width:.3f}). Awaiting direction."

        # =====================================================================
        # ANTI-CHASE AND GUARDRAIL FILTERS
        # =====================================================================
        if state in (SetupState.TRIGGERED, SetupState.ARMED):
            # Check 1: Overextended RSI Filter
            if bias == MarketBias.BULLISH and rsi > 74:
                state = SetupState.OVEREXTENDED
                reason = f"REJECTED (OVEREXTENDED): RSI is {rsi:.1f} (> 74). Chasing overbought momentum prohibited."
            elif bias == MarketBias.BEARISH and rsi < 26:
                state = SetupState.OVEREXTENDED
                reason = f"REJECTED (OVEREXTENDED): RSI is {rsi:.1f} (< 26). Chasing oversold breakdown prohibited."

            # Check 2: Too Late / Chase Distance Filter
            elif bias == MarketBias.BULLISH and (cmp - vwap) > (self.max_chase_atr_multiplier * atr):
                state = SetupState.TOO_LATE
                reason = f"REJECTED (TOO_LATE): Price is {vwap_dist_pct:.2f}% above VWAP (> {self.max_chase_atr_multiplier}x ATR). Entry missed."
            elif bias == MarketBias.BEARISH and (vwap - cmp) > (self.max_chase_atr_multiplier * atr):
                state = SetupState.TOO_LATE
                reason = f"REJECTED (TOO_LATE): Price is {abs(vwap_dist_pct):.2f}% below VWAP (> {self.max_chase_atr_multiplier}x ATR). Entry missed."

            else:
                # Check 3: Risk-to-Reward Ratio Filter
                risk_dist = abs(cmp - stop_loss)
                reward_dist = abs(target - cmp)
                rr = (reward_dist / max(risk_dist, 0.01)) if risk_dist > 0 else 0.0

                if rr < self.min_risk_reward:
                    state = SetupState.POOR_RR
                    reason = f"REJECTED (POOR_RR): Risk:Reward is {rr:.2f}:1 (minimum required: {self.min_risk_reward:.2f}:1)."

        # =====================================================================
        # CANDIDATE CREATION (ONLY IF TRIGGERED OR ARMED)
        # =====================================================================
        candidate = None
        if state in (SetupState.TRIGGERED, SetupState.ARMED) and bias != MarketBias.NEUTRAL:
            action = "CE_BUY" if bias == MarketBias.BULLISH else "PE_BUY"
            inst_type = DerivativeType.OPTIDX if index_flag else DerivativeType.OPTSTK
            risk_dist = abs(cmp - stop_loss)
            reward_dist = abs(target - cmp)
            rr = round(reward_dist / max(risk_dist, 0.01), 2)

            candidate = ScanCandidate(
                symbol=u_upper,
                underlying=u_upper,
                is_index=index_flag,
                instrument_type=inst_type,
                bias=bias,
                action=action,
                spot_price=cmp,
                entry_price=cmp,
                stop_loss=stop_loss,
                target=target,
                risk_reward_ratio=rr,
                setup_name=setup_name,
                state=state,
                reason=reason,
                atr=round(atr, 2),
                rsi=round(rsi, 1),
                vwap_distance_pct=round(vwap_dist_pct, 2),
            )

        res = ScanEvaluationResult(
            symbol=u_upper,
            underlying=u_upper,
            spot_price=cmp,
            state=state,
            bias=bias,
            reason=reason,
            candidate=candidate,
        )
        self._log_audit(res)
        return res

    def scan_universe(
        self,
        symbols: list[str],
        data_provider_callback: Any,
    ) -> tuple[list[ScanCandidate], list[ScanEvaluationResult]]:
        """
        Scan a list of symbols sequentially using a data provider callback.
        Returns: (actionable_candidates, all_evaluation_results)
        """
        candidates: list[ScanCandidate] = []
        evaluations: list[ScanEvaluationResult] = []

        for sym in symbols:
            try:
                df = data_provider_callback(sym)
                if df is not None:
                    res = self.evaluate_price_action(sym, df)
                    evaluations.append(res)
                    if res.candidate is not None:
                        candidates.append(res.candidate)
            except Exception as e:
                logger.warning(f"Error scanning {sym}: {e}")
                evaluations.append(
                    ScanEvaluationResult(
                        symbol=sym,
                        underlying=sym,
                        spot_price=0.0,
                        state=SetupState.NO_SETUP,
                        bias=MarketBias.NEUTRAL,
                        reason=f"Scan error: {type(e).__name__}",
                    )
                )

        return candidates, evaluations

    def _log_audit(self, res: ScanEvaluationResult) -> None:
        """Keep fixed-size audit log of recent evaluations."""
        entry = {
            "symbol": res.symbol,
            "spot_price": res.spot_price,
            "state": res.state.value,
            "bias": res.bias.value,
            "reason": res.reason,
            "has_candidate": res.candidate is not None,
            "evaluated_at": res.evaluated_at.isoformat(),
        }
        self._last_audit_logs.append(entry)
        if len(self._last_audit_logs) > self._max_audit_logs:
            self._last_audit_logs.pop(0)

    def get_recent_audit_logs(self) -> list[dict[str, Any]]:
        """Return audit logs of recent scanner evaluations."""
        return list(reversed(self._last_audit_logs))
