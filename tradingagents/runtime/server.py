from __future__ import annotations

from typing import Any

from .web_app import TradingWebApp


class TradingRuntimeServer:
    """Lightweight HTTP-style app interface around the safe trading runtime.

    This keeps the runtime easy to call from a local app, a future FastAPI server,
    or a simple browser wrapper without changing the safety logic itself.
    """

    def __init__(self, app: TradingWebApp | None = None) -> None:
        self.app = app or TradingWebApp()

    def start(self) -> dict[str, Any]:
        return self.app.start()

    def stop(self) -> dict[str, Any]:
        return self.app.stop()

    def health(self) -> dict[str, Any]:
        return self.app.health()

    def evaluate(self, snapshot: dict[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
        return self.app.evaluate(snapshot, symbol=symbol)
