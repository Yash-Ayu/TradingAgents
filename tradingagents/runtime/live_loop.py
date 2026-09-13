from __future__ import annotations

from typing import Any

from .market_trigger import MarketTriggerLoop


class LiveTradingLoop:
    """High-level live loop that combines the trigger gate and market logic."""

    def __init__(self, trigger: MarketTriggerLoop | None = None) -> None:
        self.trigger = trigger or MarketTriggerLoop()

    def run(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.trigger.evaluate(snapshot, symbol=symbol)
