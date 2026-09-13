from __future__ import annotations

from .http_server import TradingHTTPServer


class LocalServerRunner:
    """Start the local service object used by the app shell."""

    def __init__(self) -> None:
        self.server = TradingHTTPServer()

    def start(self):
        return self.server.start()

    def stop(self):
        return self.server.stop()

    def health(self):
        return self.server.health()


def start_local_server() -> LocalServerRunner:
    return LocalServerRunner()
