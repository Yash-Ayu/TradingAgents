"""Run a local paper-trading safety smoke test."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.runtime.broker_adapter import BrokerAdapterConfig, PaperBrokerAdapter
from tradingagents.runtime.safe_runtime import RiskGate


def evaluate_and_simulate(snapshot: dict[str, object]) -> dict[str, object]:
    decision = RiskGate().evaluate(snapshot)
    if not decision["allow_trade"]:
        return {
            "status": "blocked",
            "risk_state": decision["risk_state"],
            "reasons": decision["reasons"],
        }

    order = PaperBrokerAdapter(BrokerAdapterConfig()).place_order({
        "symbol": "NIFTY",
        "side": "BUY",
        "qty": 1,
        "price": 23400,
        "order_type": "MARKET",
    })
    return {
        "status": order["status"],
        "mode": order["mode"],
        "risk_state": decision["risk_state"],
        "order_id": order["order_id"],
    }


def main() -> None:
    safe_snapshot = {
        "is_market_open": True,
        "vix": 16,
        "drawdown_pct": 2,
        "trend_strength": 0.8,
        "atr_ratio": 0.8,
    }
    risky_snapshot = {
        "is_market_open": True,
        "vix": 42,
        "drawdown_pct": 12,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }

    print("Safe snapshot:", evaluate_and_simulate(safe_snapshot))
    print("Risky snapshot:", evaluate_and_simulate(risky_snapshot))
    print("Paper smoke complete. No live order was sent.")


if __name__ == "__main__":
    main()
