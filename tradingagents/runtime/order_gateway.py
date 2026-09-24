from __future__ import annotations

from typing import Any

from .broker_adapter import PaperBrokerAdapter


class MarketOrderGateway:
    """Broker adapter facade for a safe intraday order-management layer.

    The gateway intentionally doesnâ€™t place real orders unless a broker is
    configured. This makes the runtime safe by default and allows a future broker
    adapter to be plugged in without changing the rest of the app logic.
    """

    def __init__(self, broker_name: str | None = None, adapter: Any = None) -> None:
        self.broker_name = broker_name
        self.adapter = adapter

    @staticmethod
    def _coerce_qty(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        if self.broker_name is None:
            return {
                "status": "rejected",
                "reason": "broker_not_configured",
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "qty": order.get("qty"),
            }

        if self.broker_name.lower() != "paper":
            if self.adapter is not None and getattr(self.adapter, "supports_real_orders", False):
                return self.adapter.place_order(order)
            return {
                "status": "rejected",
                "reason": "live_order_not_implemented",
                "broker": self.broker_name,
                "mode": "live",
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "qty": order.get("qty"),
            }

        return PaperBrokerAdapter().place_order(order)
