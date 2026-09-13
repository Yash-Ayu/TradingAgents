from __future__ import annotations

from typing import Any

from .market_session import TradingDashboard


class SchedulerServer:
    """Small scheduler façade that exposes start/stop and health status."""

    def __init__(self, dashboard: TradingDashboard | None = None) -> None:
        self.dashboard = dashboard or TradingDashboard()

    def start(self) -> dict[str, Any]:
        self.dashboard.start()
        return self.health()

    def stop(self) -> dict[str, Any]:
        self.dashboard.stop()
        return self.health()

    def health(self) -> dict[str, Any]:
        status = self.dashboard.get_status()
        status["scheduler_status"] = self.dashboard.scheduler.status
        return status
