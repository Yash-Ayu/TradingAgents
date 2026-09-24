from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

SUPPORTED_BROKERS = {"paper", "angel", "angel_one", "zerodha", "upstox"}


@dataclass
class BrokerAdapterConfig:
    broker_name: str = "paper"
    api_key: str | None = None
    api_secret: str | None = None
    base_url: str | None = None
    client_id: str | None = None
    mpin: str | None = None
    totp_secret: str | None = None
    live_trading_enabled: bool = False
    live_confirmation: str | None = None
    max_order_qty: int = 50
    max_order_value: float = 100000.0

    @property
    def normalized_broker_name(self) -> str:
        name = (self.broker_name or "paper").strip().lower()
        if name in {"angel", "angel_one"}:
            return "angel"
        if name in {"paper", "demo", "simulated"}:
            return "paper"
        if name in {"zerodha", "upstox"}:
            return name
        return name


class PaperBrokerAdapter:
    """Paper-trading broker adapter for local-safe execution."""

    def __init__(self, config: BrokerAdapterConfig | None = None) -> None:
        self.config = config or BrokerAdapterConfig()
        self.broker_name = self.config.normalized_broker_name

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        try:
            qty = int(order.get("qty", 0))
            price = float(order.get("price", 0))
            valid = (
                not isinstance(order.get("qty"), bool)
                and str(qty) == str(order.get("qty")) and qty > 0
                and not isinstance(order.get("price"), bool)
                and math.isfinite(price) and price > 0
                and str(order.get("side", "")).lower() in {"buy", "sell"}
                and bool(order.get("symbol"))
            )
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            return {
                "status": "rejected", "reason": "invalid_paper_order",
                "symbol": order.get("symbol"), "broker": self.broker_name,
            }

        fill = {
            "status": "filled",
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": qty,
            "price": order.get("price"),
            "order_type": order.get("order_type", "market"),
            "mode": "paper",
        }

        return {
            "status": "accepted",
            "broker": self.broker_name,
            "mode": "paper",
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": qty,
            "filled_qty": qty,
            "order_id": str(uuid4()),
            "fills": [fill],
        }


class LiveBrokerAdapter:
    """Very small live-broker adapter contract with credentials validation."""

    def __init__(self, config: BrokerAdapterConfig | None = None) -> None:
        self.config = config or BrokerAdapterConfig(broker_name="zerodha")
        self.broker_name = self.config.normalized_broker_name

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        missing = not all([
            self.config.api_key,
            self.config.api_secret,
            self.config.base_url,
        ])

        if missing:
            return {
                "status": "rejected",
                "reason": "credentials_missing",
                "symbol": order.get("symbol"),
                "broker": self.broker_name,
                "mode": "live",
            }

        return {
            "status": "rejected",
            "reason": "live_order_not_implemented",
            "symbol": order.get("symbol"),
            "broker": self.broker_name,
            "mode": "live",
        }



class AngelOneBrokerAdapter:
    """Guarded Angel One SmartAPI order adapter.

    Real orders require every safety switch and an explicit risk-approved order.
    The adapter never reports a fill because SmartAPI order acceptance is not a
    fill confirmation; the returned order ID must be monitored separately.
    """

    LIVE_CONFIRMATION = "I_UNDERSTAND_LIVE_TRADING"
    supports_real_orders = True

    def __init__(self, config: BrokerAdapterConfig | None = None, client_factory: Any = None) -> None:
        self.config = config or BrokerAdapterConfig(broker_name="angel")
        self.broker_name = "angel"
        self._client_factory = client_factory

    def _rejected(self, reason: str, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "rejected",
            "reason": reason,
            "broker": self.broker_name,
            "mode": "live",
            "symbol": order.get("symbol"),
        }

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        config = self.config
        if not all([config.api_key, config.client_id, config.mpin, config.totp_secret]):
            return self._rejected("credentials_missing", order)
        if not config.live_trading_enabled:
            return self._rejected("live_trading_disabled", order)
        if config.live_confirmation != self.LIVE_CONFIRMATION:
            return self._rejected("live_confirmation_required", order)
        if order.get("risk_approved") is not True:
            return self._rejected("risk_not_approved", order)

        try:
            quantity = int(order.get("qty", 0))
        except (TypeError, ValueError, OverflowError):
            quantity = 0
        if isinstance(order.get("qty"), bool) or str(order.get("qty")) != str(quantity) or quantity <= 0 or quantity > config.max_order_qty:
            return self._rejected("quantity_limit_exceeded", order)

        required = ("exchange", "tradingsymbol", "symboltoken")
        if any(not order.get(field) for field in required):
            return self._rejected("instrument_details_missing", order)

        order_type = str(order.get("order_type", "MARKET")).upper()
        if order_type not in {"MARKET", "LIMIT"}:
            return self._rejected("unsupported_order_type", order)
        if str(order.get("side", "")).upper() not in {"BUY", "SELL"}:
            return self._rejected("invalid_side", order)
        try:
            price = float(order.get("price", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return self._rejected("invalid_price", order)
        if not math.isfinite(price) or price <= 0:
            return self._rejected("positive_reference_price_required", order)
        if not math.isfinite(config.max_order_value) or config.max_order_value <= 0:
            return self._rejected("invalid_value_limit", order)
        if price * quantity > config.max_order_value:
            return self._rejected("order_value_limit_exceeded", order)

        try:
            import pyotp
            from SmartApi import SmartConnect

            client_factory = self._client_factory or SmartConnect
            client = client_factory(api_key=config.api_key)
            session = client.generateSession(
                config.client_id,
                config.mpin,
                pyotp.TOTP(config.totp_secret).now(),
            )
            if not session.get("status"):
                return self._rejected("broker_login_failed", order)

            broker_order_id = client.placeOrder({
                "variety": "NORMAL",
                "tradingsymbol": order["tradingsymbol"],
                "symboltoken": str(order["symboltoken"]),
                "transactiontype": str(order.get("side", "")).upper(),
                "exchange": order["exchange"],
                "ordertype": order_type,
                "producttype": str(order.get("product_type", "MIS")).upper(),
                "duration": "DAY",
                "price": str(price if order_type == "LIMIT" else 0),
                "quantity": str(quantity),
            })
        except Exception as exc:
            return self._rejected(f"broker_order_error:{type(exc).__name__}", order)

        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            return self._rejected("broker_order_unconfirmed", order)

        return {
            "status": "accepted",
            "broker": self.broker_name,
            "mode": "live",
            "symbol": order.get("symbol", order["tradingsymbol"]),
            "order_id": broker_order_id,
            "fill_status": "unknown",
        }


def create_broker_adapter(
    config: BrokerAdapterConfig | None = None,
) -> PaperBrokerAdapter | LiveBrokerAdapter | AngelOneBrokerAdapter:
    cfg = config or BrokerAdapterConfig()
    broker_name = cfg.normalized_broker_name
    if broker_name == "paper":
        return PaperBrokerAdapter(cfg)
    if broker_name == "angel":
        return AngelOneBrokerAdapter(cfg)
    if broker_name in {"zerodha", "upstox"}:
        return LiveBrokerAdapter(cfg)
    raise ValueError("unsupported_broker")


def connect_broker(
    broker_name: str,
    api_key: str | None = None,
    api_secret: str | None = None,
    base_url: str | None = None,
    client_id: str | None = None,
    mpin: str | None = None,
    totp_secret: str | None = None,
    live_trading_enabled: bool = False,
    live_confirmation: str | None = None,
) -> dict[str, Any]:
    """Single-click connection helper for a selected broker.

    This is the app-facing entry point: you choose the broker once and the
    runtime returns the connection state. The app does not need to know the
    individual broker details beyond the config values provided here.
    """

    cfg = BrokerAdapterConfig(
        broker_name=broker_name,
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
        client_id=client_id,
        mpin=mpin,
        totp_secret=totp_secret,
        live_trading_enabled=live_trading_enabled,
        live_confirmation=live_confirmation,
    )
    normalized = cfg.normalized_broker_name

    if normalized == "paper":
        return {
            "status": "connected",
            "broker": "paper",
            "mode": "paper",
            "adapter": "PaperBrokerAdapter",
        }

    if normalized != "angel":
        return {
            "status": "not_implemented", "broker": normalized, "mode": "live",
            "reason": "live_broker_not_implemented",
        }

    credentials_ready = (
        all([api_key, client_id, mpin, totp_secret])
        if normalized == "angel"
        else all([api_key, api_secret, base_url])
    )
    if not credentials_ready:
        return {
            "status": "not_configured",
            "broker": normalized,
            "mode": "live",
            "reason": "credentials_missing",
        }

    adapter = create_broker_adapter(cfg)
    return {
        "status": "configured",
        "reason": "broker_authentication_required",
        "broker": adapter.broker_name,
        "mode": "live",
        "adapter": adapter.__class__.__name__,
        "base_url": base_url,
    }
