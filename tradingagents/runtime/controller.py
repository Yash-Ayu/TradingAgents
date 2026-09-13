from __future__ import annotations

from typing import Any

from .safe_runtime import RiskGate
from .trade_router import TradeRouter


class MarketAutomationController:
    """Controller that combines risk evaluation and broker-safe order routing.

    The controller is intentionally tiny: it evaluates the current snapshot,
    blocks risky conditions, and only routes to the trading router when the
    market is safe and a broker is configured.
    """

    def __init__(
        self,
        broker_name: str | None = None,
        gate: RiskGate | None = None,
        adapter: Any = None,
    ) -> None:
        self.gate = gate or RiskGate()
        self.router = TradeRouter(broker_name=broker_name, adapter=adapter)

    def tick(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        decision = self.gate.evaluate(snapshot)
        if not decision["allow_trade"]:
            return {
                "status": "risk_blocked",
                "action": decision.get("flatten_positions") and "flatten" or "hold",
                "symbol": symbol,
                "risk_state": decision["risk_state"],
                "reasons": decision["reasons"],
            }

        return self.router.route(snapshot, symbol=symbol)
