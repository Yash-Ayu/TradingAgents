from __future__ import annotations

from html import escape
from typing import Any


class BrowserDashboard:
    """Simple browser-ready dashboard that renders a status page."""

    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self.status = status if status is not None else {
            "status": "stopped",
            "risk_state": "unknown",
            "allow_trade": False,
            "flatten_positions": False,
            "market_open": False,
            "scheduler_status": "stopped",
        }

    def render(self) -> str:
        rows = "\n".join(
            f"<li><strong>{escape(str(key))}</strong>: {escape(str(value))}</li>"
            for key, value in self.status.items()
        )
        return f"""
<html>
  <head><title>TradingAgents Dashboard</title></head>
  <body>
    <h1>TradingAgents Dashboard</h1>
    <ul>
      {rows}
    </ul>
  </body>
</html>
"""

    def get_status(self) -> dict[str, Any]:
        return dict(self.status)
