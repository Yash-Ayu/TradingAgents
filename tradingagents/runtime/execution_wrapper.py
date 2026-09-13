from __future__ import annotations

from typing import Any

from .scheduler import SafeExecutionLoop


class SafeExecutionWrapper:
    """Execution wrapper that only allows the engine to proceed when the risk gate passes."""

    def __init__(self, loop: SafeExecutionLoop | None = None) -> None:
        self.loop = loop or SafeExecutionLoop()

    def run(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        decision = self.loop.run_cycle(snapshot)
        result = {
            "symbol": symbol,
            "allowed": decision["trade_allowed"],
            "action": decision["action"],
            "status": decision["status"],
            "risk_state": decision["risk_state"],
            "reasons": decision["reasons"],
        }
        return result
