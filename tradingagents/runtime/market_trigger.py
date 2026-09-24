from __future__ import annotations

from typing import Any

from .engine_bridge import SafeTradingEngineBridge, resolve_trade_date


class MarketTriggerLoop:
    """Loop that checks safe intraday conditions and invokes the graph when allowed."""

    def __init__(self, bridge: SafeTradingEngineBridge | None = None) -> None:
        self.bridge = bridge or SafeTradingEngineBridge()

    def evaluate(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        try:
            trade_date = resolve_trade_date(snapshot)
        except ValueError as exc:
            return {
                "symbol": symbol, "allowed": False, "action": "hold",
                "status": "invalid_analysis_context", "risk_state": "unknown",
                "reasons": [str(exc)], "decision": None,
            }
        return self.bridge.run(
            symbol=symbol,
            start_date=trade_date,
            end_date=trade_date,
            snapshot=snapshot,
        )
