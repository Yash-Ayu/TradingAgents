"""Safe execution controls for intraday trading automation.

This layer is intentionally small and explicit: it does not replace the
multi-agent research engine, but it enforces a hard safety gate before a trade is
allowed. The runtime can be used by a local app shell or a scheduler to block
risky conditions and force flattening when volatility or drawdown exceeds the
configured thresholds.
"""

from __future__ import annotations

from typing import Any


class RiskGate:
    """Deterministic intraday risk gate for trade authorization.

    The gate expects a snapshot dictionary with at least these numeric keys:
    ``is_market_open``, ``vix``, ``drawdown_pct``, ``trend_strength`` and
    ``atr_ratio``.
    """

    def __init__(
        self,
        vix_high: float = 35.0,
        vix_elevated: float = 25.0,
        drawdown_high: float = 10.0,
        drawdown_elevated: float = 7.0,
        trend_high_risk: float = 0.35,
        trend_elevated: float = 0.55,
        atr_high: float = 1.5,
        atr_elevated: float = 1.2,
    ) -> None:
        self.vix_high = vix_high
        self.vix_elevated = vix_elevated
        self.drawdown_high = drawdown_high
        self.drawdown_elevated = drawdown_elevated
        self.trend_high_risk = trend_high_risk
        self.trend_elevated = trend_elevated
        self.atr_high = atr_high
        self.atr_elevated = atr_elevated

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_bool(value: Any, default: bool = True) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on"}:
                return True
            if normalized in {"false", "0", "no", "n", "off"}:
                return False
        return default

    def evaluate(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        if snapshot is None:
            snapshot = {}

        reasons: list[str] = []
        is_market_open = self._as_bool(snapshot.get("is_market_open"), default=True)

        if not is_market_open:
            return {
                "risk_state": "closed",
                "allow_trade": False,
                "flatten_positions": True,
                "reasons": ["Market is closed"],
                "market_open": False,
            }

        vix = self._as_float(snapshot.get("vix"), default=0.0)
        drawdown = self._as_float(snapshot.get("drawdown_pct"), default=0.0)
        trend_strength = self._as_float(snapshot.get("trend_strength"), default=1.0)
        atr_ratio = self._as_float(snapshot.get("atr_ratio"), default=0.0)

        if vix >= self.vix_high:
            reasons.append("VIX is above the hard-stop threshold")
        elif vix >= self.vix_elevated:
            reasons.append("VIX is elevated")

        if drawdown >= self.drawdown_high:
            reasons.append("Portfolio drawdown is above the hard-stop threshold")
        elif drawdown >= self.drawdown_elevated:
            reasons.append("Portfolio drawdown is elevated")

        if trend_strength <= self.trend_high_risk:
            reasons.append("Trend strength is too weak for directional risk")
        elif trend_strength <= self.trend_elevated:
            reasons.append("Trend strength is weakening")

        if atr_ratio >= self.atr_high:
            reasons.append("ATR ratio is above the hard-stop threshold")
        elif atr_ratio >= self.atr_elevated:
            reasons.append("ATR ratio is elevated")

        if not reasons:
            return {
                "risk_state": "normal",
                "allow_trade": True,
                "flatten_positions": False,
                "reasons": [],
                "market_open": True,
            }

        # A single high-risk condition is enough to block new direction,
        # while multiple elevated conditions trigger a flatten signal.
        risk_state = "high" if any(
            (
                vix >= self.vix_high,
                drawdown >= self.drawdown_high,
                trend_strength <= self.trend_high_risk,
                atr_ratio >= self.atr_high,
            )
        ) else "elevated"

        return {
            "risk_state": risk_state,
            "allow_trade": False,
            "flatten_positions": True,
            "reasons": reasons,
            "market_open": True,
        }

    def should_allow_trade(self, snapshot: dict[str, Any] | None) -> bool:
        return self.evaluate(snapshot)["allow_trade"]

    def should_flatten_positions(self, snapshot: dict[str, Any] | None) -> bool:
        return self.evaluate(snapshot)["flatten_positions"]


class SafeTradingRuntime:
    """Minimal runtime for a local trading app or scheduler.

    The runtime intentionally keeps the public API tiny so it can be exposed to
    a web UI or a cron/scheduler without depending on external libraries.
    """

    def __init__(self, gate: RiskGate | None = None) -> None:
        self.gate = gate or RiskGate()
        self.status = "stopped"
        self.last_snapshot: dict[str, Any] = {
            "is_market_open": True,
            "vix": 18.0,
            "drawdown_pct": 3.0,
            "trend_strength": 0.8,
            "atr_ratio": 0.7,
        }

    def start(self) -> str:
        self.status = "running"
        return self.status

    def stop(self) -> str:
        self.status = "stopped"
        return self.status

    def update_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.last_snapshot = snapshot or {}
        return self.get_status()

    def get_status(self) -> dict[str, Any]:
        evaluation = self.gate.evaluate(self.last_snapshot)
        return {
            "status": self.status,
            "risk_state": evaluation["risk_state"],
            "allow_trade": evaluation["allow_trade"],
            "flatten_positions": evaluation["flatten_positions"],
            "reasons": evaluation["reasons"],
            "last_snapshot": self.last_snapshot,
        }

    def can_trade(self, snapshot: dict[str, Any] | None = None) -> bool:
        if snapshot is not None:
            self.last_snapshot = snapshot
        return self.gate.should_allow_trade(self.last_snapshot)
