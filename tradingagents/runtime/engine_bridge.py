from __future__ import annotations

from typing import Any, Callable

from .execution_wrapper import SafeExecutionWrapper


class SafeTradingEngineBridge:
    """Bridge between the safe runtime and the actual TradingAgents engine.

    It checks the risk gate before invoking the graph. If the risk gate blocks the
    cycle, the engine is never called and the result states that the system should
    flatten positions instead.
    """

    def __init__(self, graph_factory: Callable[[], Any] | None = None) -> None:
        self.graph_factory = graph_factory
        self.wrapper = SafeExecutionWrapper()

    def run(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        decision = self.wrapper.run(snapshot, symbol=symbol)
        if not decision["allowed"]:
            return {
                "symbol": symbol,
                "allowed": False,
                "action": decision["action"],
                "status": decision["status"],
                "risk_state": decision["risk_state"],
                "reasons": decision["reasons"],
                "decision": None,
            }

        if self.graph_factory is None:
            return {
                "symbol": symbol,
                "allowed": True,
                "action": "monitor",
                "status": "safe_to_monitor",
                "risk_state": decision["risk_state"],
                "reasons": decision["reasons"],
                "decision": "ENGINE_NOT_CONFIGURED",
            }

        graph = self.graph_factory()
        report, final_decision = graph.propagate(symbol, start_date, end_date)
        return {
            "symbol": symbol,
            "allowed": True,
            "action": "monitor",
            "status": "safe_to_monitor",
            "risk_state": decision["risk_state"],
            "reasons": decision["reasons"],
            "decision": final_decision,
            "report": report,
        }
