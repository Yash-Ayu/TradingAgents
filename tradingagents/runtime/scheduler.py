from __future__ import annotations

from typing import Any

from .safe_runtime import RiskGate


class SafeExecutionLoop:
    """One-cycle execution guard for intraday decision making.

    It evaluates a market snapshot, decides whether the system can trade, and
    returns a simple action contract that a scheduler or app can consume.
    """

    def __init__(self, gate: RiskGate | None = None) -> None:
        self.gate = gate or RiskGate()

    def run_cycle(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        decision = self.gate.evaluate(snapshot)
        if not decision["allow_trade"]:
            return {
                "trade_allowed": False,
                "action": "flatten",
                "status": "risk_blocked",
                "risk_state": decision["risk_state"],
                "reasons": decision["reasons"],
            }

        return {
            "trade_allowed": True,
            "action": "monitor",
            "status": "safe_to_monitor",
            "risk_state": decision["risk_state"],
            "reasons": decision["reasons"],
        }


class MarketScheduler:
    """Minimal scheduler for market-open monitoring loops."""

    def __init__(self, interval_seconds: int = 60) -> None:
        self.interval_seconds = interval_seconds
        self.status = "stopped"
        self.loop = SafeExecutionLoop()

    def start(self) -> str:
        self.status = "running"
        return self.status

    def stop(self) -> str:
        self.status = "stopped"
        return self.status

    def tick(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        decision = self.loop.run_cycle(snapshot)
        decision["interval_seconds"] = self.interval_seconds
        decision["scheduler_status"] = self.status
        return decision
