"""Start the guarded Angel One runtime from .env configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.runtime.app_config import launch_trading_app


def main() -> None:
    load_dotenv()
    status = launch_trading_app(
        broker_name="angel",
        api_key=os.environ.get("ANGEL_API_KEY"),
        client_id=os.environ.get("ANGEL_CLIENT_ID"),
        mpin=os.environ.get("ANGEL_MPIN"),
        totp_secret=os.environ.get("ANGEL_TOTP_SECRET"),
        live_trading_enabled=os.environ.get("ANGEL_LIVE_TRADING_ENABLED", "false").lower()
        in {"1", "true", "yes", "on"},
        live_confirmation=os.environ.get("ANGEL_LIVE_CONFIRMATION"),
    )
    print(status)


if __name__ == "__main__":
    main()
