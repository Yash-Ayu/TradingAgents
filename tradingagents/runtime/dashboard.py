from __future__ import annotations

from typing import Any

from .execution_wrapper import SafeExecutionWrapper
from .market_session import TradingDashboard as SessionDashboard


class LocalTradingDashboard:
    """Single local dashboard bridging status, market timing, and execution gate."""

    def __init__(self, wrapper: SafeExecutionWrapper | None = None) -> None:
        self.wrapper = wrapper or SafeExecutionWrapper()
        self.session_dashboard = SessionDashboard()

    def start(self) -> str:
        self.session_dashboard.start()
        return self.get_status()["status"]

    def stop(self) -> str:
        self.session_dashboard.stop()
        return self.get_status()["status"]

    def get_status(self) -> dict[str, Any]:
        session = self.session_dashboard.get_status()
        return {
            "status": session["status"],
            "market_open": session["market_open"],
            "risk_state": session["risk_state"],
            "allow_trade": session["allow_trade"],
            "flatten_positions": session["flatten_positions"],
            "scheduler_status": session["scheduler_status"],
            "reasons": session["reasons"],
        }

    def run_cycle(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.wrapper.run(snapshot, symbol=symbol)
