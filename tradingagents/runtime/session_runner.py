from __future__ import annotations

from typing import Any

from .controller import MarketAutomationController


class MarketSessionRunner:
    """High-level session runner for safe market-open automation."""

    def __init__(self, broker_name: str | None = None) -> None:
        self.controller = MarketAutomationController(broker_name=broker_name)

    def run_cycle(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.controller.tick(snapshot, symbol=symbol)

    def get_status(self) -> dict[str, Any]:
        return {
            "status": "running",
            "broker": self.controller.router.gateway.broker_name,
            "mode": "paper" if self.controller.router.gateway.broker_name == "paper" else "live",
        }
