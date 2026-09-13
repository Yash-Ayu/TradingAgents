from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .broker_adapter import BrokerAdapterConfig, connect_broker, create_broker_adapter
from .controller import MarketAutomationController
from .market_session import TradingDashboard
from .session_runner import MarketSessionRunner


@dataclass
class AppRuntimeConfig:
    """Single configuration object for starting the safe trading app."""

    broker_name: str = "paper"
    api_key: str | None = None
    api_secret: str | None = None
    base_url: str | None = None
    client_id: str | None = None
    mpin: str | None = None
    totp_secret: str | None = None
    live_trading_enabled: bool = False
    live_confirmation: str | None = None
    symbols: list[str] = field(default_factory=lambda: ["NIFTY", "BANKNIFTY", "SENSEX"])
    enable_market_session: bool = True

    def connect(self) -> dict[str, Any]:
        return connect_broker(
            self.broker_name,
            api_key=self.api_key,
            api_secret=self.api_secret,
            base_url=self.base_url,
            client_id=self.client_id,
            mpin=self.mpin,
            totp_secret=self.totp_secret,
            live_trading_enabled=self.live_trading_enabled,
            live_confirmation=self.live_confirmation,
        )

    def build_runtime(self) -> dict[str, Any]:
        connection = self.connect()
        adapter = create_broker_adapter(BrokerAdapterConfig(
            broker_name=self.broker_name,
            api_key=self.api_key,
            api_secret=self.api_secret,
            base_url=self.base_url,
            client_id=self.client_id,
            mpin=self.mpin,
            totp_secret=self.totp_secret,
            live_trading_enabled=self.live_trading_enabled,
            live_confirmation=self.live_confirmation,
        ))
        controller = MarketAutomationController(broker_name=self.broker_name, adapter=adapter)
        dashboard = TradingDashboard()
        session_runner = MarketSessionRunner(broker_name=self.broker_name)

        return {
            "status": "ready" if connection["status"] == "connected" else "not_configured",
            "broker": self.broker_name,
            "mode": connection.get("mode", "paper"),
            "symbols": self.symbols,
            "market_session": self.enable_market_session,
            "connection": connection,
            "controller": controller,
            "dashboard": dashboard,
            "session_runner": session_runner,
        }

    def start(self) -> dict[str, Any]:
        runtime = self.build_runtime()
        if runtime["status"] != "ready":
            return {
                "status": "blocked",
                "broker": self.broker_name,
                "mode": runtime.get("mode", "paper"),
                "reason": runtime["connection"].get("reason", "not_configured"),
                "symbols": self.symbols,
            }

        dashboard_status = runtime["dashboard"].get_status()
        return {
            "status": "ready",
            "broker": self.broker_name,
            "mode": runtime["mode"],
            "symbols": self.symbols,
            "market_session": self.enable_market_session,
            "risk_state": dashboard_status["risk_state"],
        }


def launch_trading_app(
    broker_name: str = "paper",
    api_key: str | None = None,
    api_secret: str | None = None,
    base_url: str | None = None,
    symbols: list[str] | None = None,
    client_id: str | None = None,
    mpin: str | None = None,
    totp_secret: str | None = None,
    live_trading_enabled: bool = False,
    live_confirmation: str | None = None,
) -> dict[str, Any]:
    config = AppRuntimeConfig(
        broker_name=broker_name,
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
        symbols=symbols or ["NIFTY", "BANKNIFTY", "SENSEX"],
        client_id=client_id,
        mpin=mpin,
        totp_secret=totp_secret,
        live_trading_enabled=live_trading_enabled,
        live_confirmation=live_confirmation,
    )
    return config.start()
