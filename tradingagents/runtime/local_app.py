from __future__ import annotations

from typing import Any

from .safe_runtime import SafeTradingRuntime


class TradingAppShell:
    """Lightweight local control shell for the safe trading runtime.

    The shell is intentionally small and framework-free so it can power a simple
    local dashboard or be wrapped by a web UI later without changing the core
    trading engine.
    """

    def __init__(self, runtime: SafeTradingRuntime | None = None) -> None:
        self.runtime = runtime or SafeTradingRuntime()
        self.status_history: list[dict[str, Any]] = []

    def start(self) -> str:
        self.runtime.start()
        return self._record_status()["status"]

    def stop(self) -> str:
        self.runtime.stop()
        return self._record_status()["status"]

    def update_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        status = self.runtime.update_snapshot(snapshot)
        self.status_history.append(status)
        return status

    def get_status(self) -> dict[str, Any]:
        status = self.runtime.get_status()
        self.status_history.append(status)
        return status

    def _record_status(self) -> dict[str, Any]:
        status = self.get_status()
        self.status_history.append(status)
        return status
