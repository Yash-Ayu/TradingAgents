"""
Secure, read-only Angel One SmartAPI client.
Phase 1 implementation: strictly handles authentication, session management,
profile connectivity, RMS funds check, and market data queries.

ABSOLUTE SAFETY INVARIANT:
Order execution APIs (placeOrder, modifyOrder, cancelOrder) are strictly prohibited
and will raise an exception if called.
"""

from contextlib import suppress
import logging
import os
from typing import Any, Dict, Optional

import pyotp
from SmartApi import SmartConnect

with suppress(Exception):
    import logzero
    logzero.logger.setLevel(logging.WARNING)
    logging.getLogger("smartapi").setLevel(logging.WARNING)
    logging.getLogger("SmartApi").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def redact_secret(val: Optional[str], show_last: int = 4) -> str:
    """Safely redact sensitive credentials for logs."""
    if not val:
        return "[NOT SET]"
    if len(val) <= show_last:
        return "****"
    return f"****{val[-show_last:]}"


class AngelOneClient:
    """
    Read-only client wrapper for Angel One SmartAPI.
    Guarantees no trade execution capability in Phase 1.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
        pin: Optional[str] = None,
        totp_secret: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("ANGEL_API_KEY", "")
        self.client_code = (
            client_code
            or os.getenv("ANGEL_CLIENT_CODE", "")
            or os.getenv("ANGEL_CLIENT_ID", "")
        )
        self.pin = pin or os.getenv("ANGEL_PIN", "") or os.getenv("ANGEL_MPIN", "")
        self.totp_secret = totp_secret or os.getenv("ANGEL_TOTP_SECRET", "")

        self._smart_connect: Optional[SmartConnect] = None
        self._jwt_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._is_authenticated = False

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    def authenticate(self) -> Dict[str, Any]:
        """
        Authenticate with Angel One SmartAPI using TOTP.
        Never prints or logs secret credentials.
        """
        if not self.api_key or not self.client_code or not self.pin or not self.totp_secret:
            missing = []
            if not self.api_key:
                missing.append("ANGEL_API_KEY")
            if not self.client_code:
                missing.append("ANGEL_CLIENT_CODE (or ANGEL_CLIENT_ID)")
            if not self.pin:
                missing.append("ANGEL_PIN (or ANGEL_MPIN)")
            if not self.totp_secret:
                missing.append("ANGEL_TOTP_SECRET")
            err = f"Missing required credentials: {', '.join(missing)}"
            logger.error(err)
            return {"status": False, "message": err, "error_code": "MISSING_CREDENTIALS"}

        try:
            logger.info(f"Initiating authentication for client: {redact_secret(self.client_code)}...")
            self._smart_connect = SmartConnect(api_key=self.api_key)

            # Generate TOTP
            totp = pyotp.TOTP(self.totp_secret).now()

            # Generate Session
            session_data = self._smart_connect.generateSession(
                clientCode=self.client_code,
                password=self.pin,
                totp=totp,
            )

            if not session_data or not session_data.get("status"):
                err_msg = session_data.get("message", "Authentication failed without error message.")
                logger.error(f"Angel One authentication failed: {err_msg}")
                self._is_authenticated = False
                return {
                    "status": False,
                    "message": err_msg,
                    "error_code": session_data.get("errorcode", "AUTH_FAILED"),
                }

            data = session_data.get("data", {})
            self._jwt_token = data.get("jwtToken")
            self._refresh_token = data.get("refreshToken")
            self._feed_token = data.get("feedToken")
            self._is_authenticated = True

            logger.info("Angel One authentication successful. Session established.")
            return {
                "status": True,
                "message": "Authentication successful",
                "client_code_masked": redact_secret(self.client_code),
            }

        except Exception as e:
            logger.error(f"Exception during Angel One authentication: {type(e).__name__}")
            self._is_authenticated = False
            return {"status": False, "message": str(e), "error_code": "EXCEPTION"}

    def get_profile(self) -> Dict[str, Any]:
        """Fetch user profile with sensitive fields redacted."""
        if not self._is_authenticated or not self._smart_connect:
            return {"status": False, "message": "Not authenticated. Call authenticate() first."}

        try:
            profile = self._smart_connect.getProfile(self._refresh_token)
            if profile and profile.get("status"):
                data = profile.get("data", {})
                # Mask sensitive fields
                safe_data = {
                    "client_code": redact_secret(data.get("clientcode")),
                    "name": data.get("name"),
                    "email": redact_secret(data.get("email"), show_last=6),
                    "exchanges": data.get("exchanges", []),
                    "products": data.get("products", []),
                }
                return {"status": True, "data": safe_data}
            return profile
        except Exception as e:
            return {"status": False, "message": f"Failed to fetch profile: {e}"}

    def get_rms(self) -> Dict[str, Any]:
        """Fetch Risk Management System (RMS) / Funds & Margin data (read-only)."""
        if not self._is_authenticated or not self._smart_connect:
            return {"status": False, "message": "Not authenticated. Call authenticate() first."}

        try:
            rms_data = self._smart_connect.getRMS()
            return rms_data
        except Exception as e:
            return {"status": False, "message": f"Failed to fetch RMS: {e}"}

    def get_market_data(self, mode: str, exchange_tokens: Dict[str, list]) -> Dict[str, Any]:
        """Fetch market data snapshot (read-only)."""
        if not self._is_authenticated or not self._smart_connect:
            return {"status": False, "message": "Not authenticated. Call authenticate() first."}

        try:
            return self._smart_connect.getMarketData(mode=mode, exchangeTokens=exchange_tokens)
        except Exception as e:
            return {"status": False, "message": f"Failed to fetch market data: {e}"}

    # =========================================================================
    # STRICT SAFETY INVARIANT: EXECUTION APIs ARE PERMANENTLY BLOCKED IN PHASE 1
    # =========================================================================
    def place_order(self, *args, **kwargs):
        raise NotImplementedError(
            "CRITICAL SAFETY VIOLATION: Order placement is strictly prohibited in Phase 1."
        )

    def modify_order(self, *args, **kwargs):
        raise NotImplementedError(
            "CRITICAL SAFETY VIOLATION: Order modification is strictly prohibited in Phase 1."
        )

    def cancel_order(self, *args, **kwargs):
        raise NotImplementedError(
            "CRITICAL SAFETY VIOLATION: Order cancellation is strictly prohibited in Phase 1."
        )

