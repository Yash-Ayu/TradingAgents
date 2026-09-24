from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .execution_wrapper import SafeExecutionWrapper


def resolve_trade_date(
    snapshot: dict[str, Any], *, allow_historical: bool = True,
    now: datetime | None = None,
) -> str:
    """Use the Indian session date; historical dates are analysis/paper-only."""
    today = (now or datetime.now(ZoneInfo("Asia/Kolkata"))).astimezone(
        ZoneInfo("Asia/Kolkata")
    ).date()
    supplied = snapshot.get("trade_date")
    if supplied is None:
        return today.isoformat()
    if not isinstance(supplied, str):
        raise ValueError("invalid_trade_date")
    try:
        selected = date.fromisoformat(supplied)
    except ValueError:
        raise ValueError("invalid_trade_date") from None
    if selected.isoformat() != supplied:
        raise ValueError("invalid_trade_date")
    if selected > today:
        raise ValueError("future_trade_date")
    if not allow_historical and selected != today:
        raise ValueError("historical_date_not_allowed_for_live_orders")
    return supplied


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
        # Keep the legacy range arguments for callers, but the engine analyzes
        # one date only: end_date. start_date is not a data-fetch window.
        try:
            trade_date = resolve_trade_date({"trade_date": end_date})
            asset_type = snapshot.get("asset_type", "stock")
            if asset_type not in {"stock", "crypto"}:
                raise ValueError("invalid_asset_type")
        except ValueError as exc:
            return {
                "symbol": symbol, "allowed": False, "action": "hold",
                "status": "invalid_analysis_context", "risk_state": "unknown",
                "reasons": [str(exc)], "decision": None,
            }
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
        report, final_decision = graph.propagate(symbol, trade_date, asset_type=asset_type)
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
