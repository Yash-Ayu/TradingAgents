from __future__ import annotations

from typing import Any

from .engine_bridge import SafeTradingEngineBridge


class MarketTriggerLoop:
    """Loop that checks safe intraday conditions and invokes the graph when allowed."""

    def __init__(self, bridge: SafeTradingEngineBridge | None = None) -> None:
        self.bridge = bridge or SafeTradingEngineBridge()

    def evaluate(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.bridge.run(
            symbol=symbol,
            start_date="2025-01-01",
            end_date="2026-09-01",
            snapshot=snapshot,
        )
