from __future__ import annotations

from typing import Any

from .web_app import TradingWebApp


class TradingHTTPServer:
    """Small HTTP-style server wrapper for local app access.

    This provides a clean interface for status and control calls without bringing
    in external web frameworks. It is intentionally minimal and safe by default.
    """

    def __init__(self, app: TradingWebApp | None = None) -> None:
        self.app = app or TradingWebApp()

    def start(self) -> dict[str, Any]:
        return self.app.start()

    def stop(self) -> dict[str, Any]:
        return self.app.stop()

    def health(self) -> dict[str, Any]:
        status = self.app.health()
        status["controls"] = {"start": "start", "stop": "stop"}
        return status

    def evaluate(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.app.evaluate(snapshot, symbol=symbol)
