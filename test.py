"""Angel One SmartAPI login script.

This script uses SmartConnect and pyotp to:
1) generate the TOTP automatically from the TOTP secret token,
2) log in with API key + client ID + MPIN,
3) print the session tokens: jwtToken and feedToken.

Replace the placeholder values before running this script.
"""

from __future__ import annotations

import os

import pyotp
from dotenv import load_dotenv
from SmartApi import SmartConnect

load_dotenv()

API_KEY = os.environ.get("ANGEL_API_KEY")
CLIENT_ID = os.environ.get("ANGEL_CLIENT_ID")
MPIN = os.environ.get("ANGEL_MPIN")
TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET")


def main() -> None:
    try:
        missing = [
            name
            for name, value in {
                "ANGEL_API_KEY": API_KEY,
                "ANGEL_CLIENT_ID": CLIENT_ID,
                "ANGEL_MPIN": MPIN,
                "ANGEL_TOTP_SECRET": TOTP_SECRET,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing environment variable(s): {', '.join(missing)}")

        smart_connect = SmartConnect(api_key=API_KEY)

        # Auto-generate 6-digit OTP from the secret token
        otp = pyotp.TOTP(TOTP_SECRET).now()

        # Login using client ID, MPIN, and TOTP
        session = smart_connect.generateSession(
            CLIENT_ID,
            MPIN,
            otp,
        )

        data = session.get("data", {})
        jwt_token = data.get("jwtToken")
        feed_token = data.get("feedToken")

        if not session.get("status"):
            print(f"Login failed: {session.get('message', 'Unknown broker error')}")
            return

        print("Login successful")
        print(f"jwtToken received: {'yes' if jwt_token else 'no'}")
        print(f"feedToken received: {'yes' if feed_token else 'no'}")
        print("Tokens are kept hidden for security.")

    except Exception as exc:
        print(f"Login failed: {exc}")


if __name__ == "__main__":
    main()
