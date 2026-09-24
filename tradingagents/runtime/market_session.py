from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from .safe_runtime import SafeTradingRuntime
from .scheduler import MarketScheduler


class MarketSessionMonitor:
    """Track Indian market open/close windows for intraday trading."""

    def __init__(self, market_id: str = "india_nse", holidays: set[date] | None = None) -> None:
        self.market_id = market_id
        self.market_hours = {
            "india_nse": {"open": time(9, 15), "close": time(15, 30)},
            "india_bse": {"open": time(9, 15), "close": time(15, 30)},
            "us_nasdaq": {"open": time(9, 30), "close": time(16, 0)},
        }

        if market_id not in self.market_hours:
            raise ValueError(f"Unsupported market: {market_id}")
        self.timezone = ZoneInfo("America/New_York" if market_id == "us_nasdaq" else "Asia/Kolkata")
        self.holidays = set(holidays or ())

    def is_market_open(self, current_time: datetime | None = None) -> bool:
        now = current_time or datetime.now(self.timezone)
        if now.tzinfo is not None:
            now = now.astimezone(self.timezone)
        if now.weekday() >= 5 or now.date() in self.holidays:
            return False
        hours = self.market_hours[self.market_id]
        return hours["open"] <= now.time() < hours["close"]


class TradingDashboard:
    """Minimal dashboard object that aggregates runtime health and market state."""

    def __init__(self, runtime: SafeTradingRuntime | None = None, scheduler: MarketScheduler | None = None) -> None:
        self.runtime = runtime or SafeTradingRuntime()
        self.scheduler = scheduler or MarketScheduler()
        self.session = MarketSessionMonitor()

    def start(self) -> str:
        self.scheduler.start()
        self.runtime.start()
        return self.get_status()["status"]

    def stop(self) -> str:
        self.scheduler.stop()
        self.runtime.stop()
        return self.get_status()["status"]

    def get_status(self) -> dict[str, Any]:
        market_open = self.session.is_market_open()
        runtime_status = self.runtime.get_status()
        scheduler_status = self.scheduler.status
        return {
            "status": runtime_status["status"],
            "market_open": market_open,
            "risk_state": runtime_status["risk_state"],
            "allow_trade": market_open and runtime_status["allow_trade"],
            "flatten_positions": runtime_status["flatten_positions"],
            "scheduler_status": scheduler_status,
            "reasons": runtime_status["reasons"],
        }
