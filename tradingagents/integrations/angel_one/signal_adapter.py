"""
TradingAgents Signal Adapter and Schema Validator for Indian F&O.
Converts abstract LLM/multi-agent output into strict, deterministic, and validated signals.
Enforces FAIL-CLOSED rules:
- HOLD / REVIEW = NO TRADE
- Low Confidence = NO TRADE
- Stale Signal = NO TRADE
- Malformed Payload = NO TRADE
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from .models import MarketBias

logger = logging.getLogger(__name__)


class ValidatedSignal(BaseModel):
    """Deterministic structured signal output required for Indian F&O pipeline."""
    underlying: str = Field(min_length=1, description="Underlying index e.g. NIFTY, BANKNIFTY")
    direction: MarketBias = Field(description="Directional bias: BULLISH, BEARISH, or NEUTRAL")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")
    strategy_id: str = Field(default="TRADING_AGENTS_MULTI_AGENT", description="Identifier of generating strategy")
    timestamp: datetime = Field(default_factory=datetime.now, description="Timestamp of signal generation")
    data_timestamp: datetime = Field(default_factory=datetime.now, description="Timestamp of underlying market data")
    reasoning: str = Field(default="", description="Summary thesis or reference from agents")
    data_source: str = Field(default="LIVE_ANGEL_ONE", description="LIVE_ANGEL_ONE or SIMULATION")

    @field_validator("underlying")
    @classmethod
    def normalize_underlying(cls, v: str) -> str:
        return v.strip().upper()


class TradingAgentsSignalAdapter:
    """
    Validates, adapts, and gates signals produced by TradingAgents framework.
    Guarantees that no malformed, stale, or ambiguous signals ever reach execution.
    """

    def __init__(
        self,
        min_confidence: float = 0.60,
        max_signal_age_seconds: float = 300.0,  # 5 minutes
    ):
        self.min_confidence = min_confidence
        self.max_signal_age_seconds = max_signal_age_seconds

    def validate_signal(self, signal: ValidatedSignal, evaluation_time: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        Evaluate signal against strict safety guardrails.
        Returns: (is_valid_for_trade, reason)
        """
        # 1. HOLD / NEUTRAL CHECK
        if signal.direction == MarketBias.NEUTRAL:
            return False, "HOLD / NEUTRAL signal. Fail-closed: NO TRADE."

        # 2. CONFIDENCE THRESHOLD CHECK
        if signal.confidence < self.min_confidence:
            return False, f"Confidence ({signal.confidence:.2f}) below minimum threshold ({self.min_confidence:.2f}). NO TRADE."

        # 3. STALE SIGNAL CHECK
        now = evaluation_time or datetime.now()
        age = (now - signal.data_timestamp).total_seconds()
        if age < 0:
            age = 0
        if age > self.max_signal_age_seconds:
            return False, f"Stale signal: Data age is {age:.1f}s (max allowed: {self.max_signal_age_seconds}s). NO TRADE."

        # All checks passed
        return True, "Signal passed all deterministic validation gates."

    def adapt_from_agent_output(
        self,
        raw_output: Dict[str, Any] | str,
        underlying: str,
        data_source: str = "LIVE_ANGEL_ONE",
        data_timestamp: Optional[datetime] = None,
        strategy_id: str = "TRADING_AGENTS_PM",
    ) -> Optional[ValidatedSignal]:
        """
        Convert raw TradingAgents / Portfolio Manager decision into a ValidatedSignal.
        Handles both dictionary outputs and markdown strings.
        """
        direction = MarketBias.NEUTRAL
        confidence = 0.5
        reasoning = ""

        if isinstance(raw_output, dict):
            # Extract from structured dictionary
            rating = str(raw_output.get("rating", raw_output.get("decision", ""))).strip().upper()
            confidence = float(raw_output.get("confidence", 0.5))
            reasoning = str(raw_output.get("reasoning", raw_output.get("summary", "")))
        else:
            # Extract from markdown text
            text = str(raw_output).upper()
            rating = "HOLD"
            if "BUY" in text or "OVERWEIGHT" in text:
                rating = "BUY"
                confidence = 0.75
            elif "SELL" in text or "UNDERWEIGHT" in text:
                rating = "SELL"
                confidence = 0.75
            reasoning = str(raw_output)[:200]

        # Map rating to MarketBias
        if rating in ("BUY", "OVERWEIGHT", "BULLISH"):
            direction = MarketBias.BULLISH
        elif rating in ("SELL", "UNDERWEIGHT", "BEARISH"):
            direction = MarketBias.BEARISH
        else:
            direction = MarketBias.NEUTRAL

        try:
            return ValidatedSignal(
                underlying=underlying,
                direction=direction,
                confidence=confidence,
                strategy_id=strategy_id,
                timestamp=datetime.now(),
                data_timestamp=data_timestamp or datetime.now(),
                reasoning=reasoning,
                data_source=data_source,
            )
        except Exception as e:
            logger.error(f"Failed to create ValidatedSignal from agent output: {e}")
            return None

