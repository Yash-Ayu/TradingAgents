from __future__ import annotations

from typing import Any

from .ui_app import TradingAppUI


class TradingWebApp:
    """Very small web-facing wrapper around the local runtime.

    This is intentionally lightweight and framework-free so it can be served by a
    simple HTTP server or adopted by FastAPI/Flask later without changing the
    safety logic underneath.
    """

    def __init__(self, ui: TradingAppUI | None = None) -> None:
        self.ui = ui or TradingAppUI()

    def start(self) -> dict[str, Any]:
        return {"status": self.ui.start(), "controls": {"start": "start", "stop": "stop"}}

    def stop(self) -> dict[str, Any]:
        return {"status": self.ui.stop(), "controls": {"start": "start", "stop": "stop"}}

    def health(self) -> dict[str, Any]:
        status = self.ui.get_status()
        status["controls"] = {"start": "start", "stop": "stop"}
        return status

    def evaluate(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.ui.run_cycle(snapshot, symbol=symbol)
