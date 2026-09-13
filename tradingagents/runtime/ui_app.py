from __future__ import annotations

from typing import Any

from .dashboard import LocalTradingDashboard


class TradingAppUI:
    """Minimal UI-facing wrapper over the local dashboard.

    This intentionally stays lightweight so it can be served by a terminal app,
    browser UI, or later replaced with a richer frontend without changing the
    underlying safety logic.
    """

    def __init__(self, dashboard: LocalTradingDashboard | None = None) -> None:
        self.dashboard = dashboard or LocalTradingDashboard()

    def start(self) -> str:
        return self.dashboard.start()

    def stop(self) -> str:
        return self.dashboard.stop()

    def get_status(self) -> dict[str, Any]:
        status = self.dashboard.get_status()
        status["controls"] = {"start": "start", "stop": "stop"}
        return status

    def run_cycle(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.dashboard.run_cycle(snapshot, symbol=symbol)
