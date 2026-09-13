from __future__ import annotations

from typing import Any

from .engine_bridge import SafeTradingEngineBridge
from .order_gateway import MarketOrderGateway


class TradeRouter:
    """Top-level router that enforces both risk and broker clearance before any trade."""

    def __init__(
        self,
        broker_name: str | None = None,
        bridge: SafeTradingEngineBridge | None = None,
        adapter: Any = None,
    ) -> None:
        self.gateway = MarketOrderGateway(broker_name=broker_name, adapter=adapter)
        self.bridge = bridge or SafeTradingEngineBridge()

    def route(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        bridge_result = self.bridge.run(
            symbol=symbol,
            start_date="2025-01-01",
            end_date="2026-09-01",
            snapshot=snapshot,
        )

        if not bridge_result["allowed"]:
            return {
                "status": "blocked",
                "action": bridge_result["action"],
                "symbol": symbol,
                "risk_state": bridge_result["risk_state"],
                "reason": "risk_blocked",
            }

        order = {
            "symbol": symbol,
            "side": "buy",
            "qty": 10,
            "risk_approved": True,
            **(snapshot.get("live_order") or {}),
        }
        gateway_result = self.gateway.place_order(order)

        if gateway_result["status"] != "accepted":
            return {
                "status": "blocked",
                "action": "hold",
                "symbol": symbol,
                "risk_state": bridge_result["risk_state"],
                "reason": gateway_result["reason"],
            }

        return {
            "status": "accepted",
            "action": "monitor",
            "symbol": symbol,
            "risk_state": bridge_result["risk_state"],
            "broker": gateway_result["broker"],
            "mode": gateway_result.get("mode", "paper"),
            "decision": bridge_result.get("decision"),
            "fills": gateway_result.get("fills", []),
        }
