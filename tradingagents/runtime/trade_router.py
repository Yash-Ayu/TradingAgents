from __future__ import annotations

import math
from typing import Any

from .engine_bridge import SafeTradingEngineBridge, resolve_trade_date
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
        try:
            trade_date = resolve_trade_date(
                snapshot, allow_historical=self.gateway.broker_name == "paper",
            )
        except ValueError as exc:
            return {
                "status": "blocked", "action": "hold", "symbol": symbol,
                "risk_state": "unknown", "reason": str(exc),
            }
        bridge_result = self.bridge.run(
            symbol=symbol,
            start_date=trade_date,
            end_date=trade_date,
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

        signal = str(bridge_result.get("decision", "")).strip().upper()
        # Overweight/Underweight need portfolio-aware sizing; do not turn
        # relative allocations or unrecognized text into market orders.
        if signal not in {"BUY", "SELL"}:
            return {
                "status": "blocked", "action": "hold", "symbol": symbol,
                "risk_state": bridge_result["risk_state"],
                "reason": "no_actionable_engine_decision",
            }
        proposal = snapshot.get("order") or snapshot.get("live_order")
        if not isinstance(proposal, dict) or not proposal.get("qty") or not proposal.get("price"):
            return {
                "status": "blocked", "action": "hold", "symbol": symbol,
                "risk_state": bridge_result["risk_state"],
                "reason": "explicit_order_required",
            }
        try:
            qty = int(proposal["qty"])
            price = float(proposal["price"])
            valid = (
                not isinstance(proposal["qty"], bool)
                and str(qty) == str(proposal["qty"]) and qty > 0
                and not isinstance(proposal["price"], bool)
                and math.isfinite(price) and price > 0
            )
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid or proposal.get("symbol", symbol) != symbol:
            return {
                "status": "blocked", "action": "hold", "symbol": symbol,
                "risk_state": bridge_result["risk_state"], "reason": "invalid_order",
            }
        order = {
            **proposal,
            "symbol": symbol,
            "side": signal.lower(),
            "qty": qty,
            "price": price,
            "risk_approved": True,
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
            "order_id": gateway_result.get("order_id"),
            "fill_status": gateway_result.get("fill_status"),
        }
