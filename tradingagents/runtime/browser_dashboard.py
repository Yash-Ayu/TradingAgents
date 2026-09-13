from __future__ import annotations

from typing import Any


class BrowserDashboard:
    """Simple browser-ready dashboard that renders a status page."""

    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self.status = status or {
            "status": "running",
            "risk_state": "normal",
            "allow_trade": True,
            "flatten_positions": False,
            "market_open": True,
            "scheduler_status": "running",
        }

    def render(self) -> str:
        rows = "\n".join(
            f"<li><strong>{key}</strong>: {value}</li>" for key, value in self.status.items()
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
