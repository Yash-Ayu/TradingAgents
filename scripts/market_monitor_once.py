"""Evaluate one safe market-monitoring cycle without placing an order."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.runtime.market_session import MarketSessionMonitor
from tradingagents.runtime.scheduler import MarketScheduler


def main() -> None:
    session = MarketSessionMonitor()
    scheduler = MarketScheduler(interval_seconds=60)
    scheduler.start()

    snapshot = {
        "is_market_open": session.is_market_open(),
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.9,
    }
    result = scheduler.tick(snapshot)
    print(result)
    print("Monitor cycle complete. No order was placed.")


if __name__ == "__main__":
    main()
