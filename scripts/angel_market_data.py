"""Fetch one Angel One quote without placing an order."""

from __future__ import annotations

import os

import pyotp
from dotenv import load_dotenv
from SmartApi import SmartConnect

load_dotenv()


REQUIRED_VARS = ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")


def main() -> None:
    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing environment variable(s): {', '.join(missing)}")

    exchange = os.environ.get("ANGEL_EXCHANGE", "NSE")
    trading_symbol = os.environ.get("ANGEL_TRADING_SYMBOL", "NIFTY 50")
    symbol_token = os.environ.get("ANGEL_SYMBOL_TOKEN", "99926000")

    smart_connect = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    session = smart_connect.generateSession(
        os.environ["ANGEL_CLIENT_ID"],
        os.environ["ANGEL_MPIN"],
        pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now(),
    )
    if not session.get("status"):
        raise SystemExit(f"Login failed: {session.get('message', 'Unknown broker error')}")

    quote = smart_connect.ltpData(exchange, trading_symbol, symbol_token)
    if not quote.get("status"):
        raise SystemExit(f"Market-data request failed: {quote.get('message', 'Unknown broker error')}")

    data = quote.get("data") or {}
    print("Market data received")
    print(f"symbol: {data.get('tradingSymbol', trading_symbol)}")
    print(f"exchange: {data.get('exchange', exchange)}")
    print(f"ltp: {data.get('ltp', 'unavailable')}")
    print(f"open: {data.get('open', 'unavailable')}")
    print(f"high: {data.get('high', 'unavailable')}")
    print(f"low: {data.get('low', 'unavailable')}")
    print(f"close: {data.get('close', 'unavailable')}")
    print("No order was placed.")


if __name__ == "__main__":
    main()
