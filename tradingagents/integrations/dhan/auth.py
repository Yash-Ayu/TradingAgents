"""
DhanHQ Automatic Authentication and Token Lifecycle Manager.
Implements automated access token generation via official TOTP flow,
background pre-expiry refresh, bounded backoff, thread-safe locking,
and strict credential redaction.

SAFETY INVARIANTS:
1. Zero secrets or tokens are ever logged, printed, or exposed in status APIs.
2. Thread-safe lock prevents concurrent scanner threads from hammering auth endpoint.
3. Bounded exponential backoff prevents hot-looping on auth/network/rate-limit failures.
4. On failure, fails closed without granting live execution rights.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from tradingagents.runtime.security import register_secret

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class DhanAuthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    NOT_CONFIGURED = "not_configured"
    EXPIRED = "expired"


def generate_totp(secret: str) -> str:
    """
    Generate standard 6-digit TOTP code (RFC 6238) from base32 secret.
    Uses pyotp if available, with robust standard-library HMAC fallback.
    """
    clean_secret = (secret or "").strip().replace(" ", "").upper()
    if not clean_secret:
        raise ValueError("empty_totp_secret")

    # 1. Try pyotp if installed
    try:
        import pyotp
        return pyotp.TOTP(clean_secret).now()
    except Exception:
        pass

    # 2. Standard library RFC 6238 HMAC-SHA1 fallback
    try:
        # Pad base32 string to multiple of 8 if needed
        missing_padding = len(clean_secret) % 8
        if missing_padding:
            clean_secret += "=" * (8 - missing_padding)
        key = base64.b32decode(clean_secret, casefold=True)
        counter = int(time.time() // 30)
        msg = struct.pack(">Q", counter)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{code:06d}"
    except Exception as exc:
        raise ValueError(f"failed_to_generate_totp: {type(exc).__name__}") from exc


class DhanTokenManager:
    """
    Thread-safe manager for Dhan access token lifecycle.
    Automates 24-hour token generation via TOTP, caching, and auto-refresh.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        pin: Optional[str] = None,
        totp_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_threshold_sec: float = 1800.0,
        base_auth_url: str = "https://auth.dhan.co/app/generateAccessToken",
    ):
        self._client_id = (client_id or os.environ.get("DHAN_CLIENT_ID", "")).strip()
        self._pin = (pin or os.environ.get("DHAN_PIN", "")).strip()
        self._totp_secret = (totp_secret or os.environ.get("DHAN_TOTP_SECRET", "")).strip()
        self._manual_token = (access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")).strip()

        self.refresh_threshold_sec = refresh_threshold_sec
        self.base_auth_url = base_auth_url

        self._lock = threading.RLock()
        self._cached_token: Optional[str] = None
        self._token_generated_at: Optional[datetime] = None
        self._token_expiry: Optional[datetime] = None

        self._consecutive_failures: int = 0
        self._cooldown_until_mono: float = 0.0
        self._last_error: Optional[str] = None

        # Register existing secrets immediately
        if self._client_id:
            register_secret(self._client_id)
        if self._pin:
            register_secret(self._pin)
        if self._totp_secret:
            register_secret(self._totp_secret)
        if self._manual_token:
            register_secret(self._manual_token)

        if self.is_configured():
            if self._manual_token and not (self._pin and self._totp_secret):
                # Manual static token flow
                self._cached_token = self._manual_token
                self._token_generated_at = datetime.now(IST)
                self._token_expiry = self._token_generated_at + timedelta(hours=24)
                self._state = DhanAuthState.HEALTHY
            else:
                self._state = DhanAuthState.HEALTHY
        else:
            self._state = DhanAuthState.NOT_CONFIGURED

    def is_configured(self) -> bool:
        """Return True if sufficient credentials exist for either automated TOTP or manual token."""
        has_totp = bool(self._client_id and self._pin and self._totp_secret)
        has_manual = bool(self._manual_token and self._manual_token.lower() not in {"placeholder", "none", ""})
        return has_totp or has_manual

    def has_totp_auth(self) -> bool:
        """Return True if credentials exist for automated TOTP token generation."""
        return bool(self._client_id and self._pin and self._totp_secret)

    @property
    def state(self) -> DhanAuthState:
        if not self.is_configured():
            return DhanAuthState.NOT_CONFIGURED
        return self._state

    def is_token_valid(self) -> bool:
        """Check if cached token exists and has not expired."""
        if not self._cached_token or not self._token_expiry:
            return False
        return datetime.now(IST) < self._token_expiry

    def get_remaining_lifetime_sec(self) -> float:
        """Seconds remaining before cached token expires, or 0.0 if expired/invalid."""
        if not self.is_token_valid():
            return 0.0
        assert self._token_expiry is not None
        delta = (self._token_expiry - datetime.now(IST)).total_seconds()
        return max(0.0, delta)

    def mark_token_invalid(self, reason: Optional[str] = None) -> None:
        """Explicitly invalidate current token (e.g. upon HTTP 401 from broker)."""
        with self._lock:
            self._cached_token = None
            self._token_expiry = None
            self._state = DhanAuthState.EXPIRED
            if reason:
                self._last_error = reason
            logger.info(f"Dhan token invalidated: {reason or 'broker_rejected'}")

    def get_valid_access_token(self, force_refresh: bool = False) -> str:
        """
        Thread-safe access token retriever.
        Automatically generates or refreshes token if absent, expired, or near expiry.
        """
        if not self.is_configured():
            self._state = DhanAuthState.NOT_CONFIGURED
            raise ValueError("dhan_not_configured")

        # Fast path check outside lock
        if not force_refresh and self.is_token_valid():
            if self.get_remaining_lifetime_sec() > self.refresh_threshold_sec:
                assert self._cached_token is not None
                return self._cached_token

        with self._lock:
            # Re-check under lock (double-checked locking)
            if not force_refresh and self.is_token_valid():
                if self.get_remaining_lifetime_sec() > self.refresh_threshold_sec:
                    assert self._cached_token is not None
                    return self._cached_token

            # Verify rate-limit / backoff cooldown
            now_mono = time.monotonic()
            if now_mono < self._cooldown_until_mono:
                # If cached token is still legally unexpired, return it with a warning
                if self.is_token_valid():
                    logger.warning("Dhan auth in cooldown; serving existing unexpired token.")
                    assert self._cached_token is not None
                    return self._cached_token
                raise ValueError(f"dhan_auth_cooldown_active: {self._last_error}")

            # Automated TOTP generation flow
            if self.has_totp_auth():
                return self._request_fresh_token_locked()

            # Manual token fallback
            if self._manual_token:
                self._cached_token = self._manual_token
                self._token_generated_at = datetime.now(IST)
                self._token_expiry = self._token_generated_at + timedelta(hours=24)
                self._state = DhanAuthState.HEALTHY
                return self._cached_token

            self._state = DhanAuthState.NOT_CONFIGURED
            raise ValueError("dhan_credentials_missing")

    def _request_fresh_token_locked(self) -> str:
        """
        Execute official Dhan TOTP token generation POST request.
        Must be called under self._lock. Never logs secret query parameters.
        """
        try:
            totp_code = generate_totp(self._totp_secret)
        except Exception as exc:
            self._state = DhanAuthState.AUTH_FAILED
            self._last_error = f"totp_generation_failed: {type(exc).__name__}"
            self._schedule_backoff(30.0)
            raise ValueError(f"dhan_totp_error: {self._last_error}") from exc

        # Official Dhan auth endpoint uses query params on POST
        params = urllib.parse.urlencode({
            "dhanClientId": self._client_id,
            "pin": self._pin,
            "totp": totp_code,
        })
        url = f"{self.base_auth_url}?{params}"

        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "User-Agent": "TradingAgents/2.0",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_bytes = resp.read()
                data = json.loads(resp_bytes.decode("utf-8"))

            token = data.get("accessToken")
            if not token:
                remarks = data.get("remarks") or data.get("errorType") or "no_access_token_in_response"
                raise ValueError(f"auth_rejected: {remarks}")

            register_secret(token)
            self._cached_token = token
            self._token_generated_at = datetime.now(IST)

            # Parse expiryTime if provided
            expiry_str = data.get("expiryTime")
            if expiry_str:
                try:
                    # e.g. "2026-01-01T00:00:00.000"
                    clean_dt = expiry_str.replace("Z", "").replace(" ", "T")
                    parsed = datetime.fromisoformat(clean_dt)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=IST)
                    self._token_expiry = parsed
                except Exception:
                    self._token_expiry = self._token_generated_at + timedelta(hours=23, minutes=50)
            else:
                self._token_expiry = self._token_generated_at + timedelta(hours=23, minutes=50)

            self._state = DhanAuthState.HEALTHY
            self._consecutive_failures = 0
            self._cooldown_until_mono = 0.0
            self._last_error = None

            logger.info("Dhan access token generated successfully via TOTP auth flow.")
            return token

        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 429:
                self._state = DhanAuthState.RATE_LIMITED
                self._last_error = "rate_limited_429"
                cooldown = 60.0 * (2 ** min(self._consecutive_failures, 3))
            elif code in (400, 401, 403):
                self._state = DhanAuthState.AUTH_FAILED
                self._last_error = f"auth_failed_http_{code}"
                cooldown = 30.0
            else:
                self._state = DhanAuthState.DEGRADED
                self._last_error = f"http_error_{code}"
                cooldown = 10.0 * (2 ** min(self._consecutive_failures, 3))

            self._schedule_backoff(cooldown)
            logger.warning(f"Dhan auth request failed: {self._last_error} (cooldown {cooldown:.0f}s)")
            raise ValueError(f"dhan_auth_http_error: {self._last_error}") from exc

        except urllib.error.URLError as exc:
            self._state = DhanAuthState.NETWORK_ERROR
            self._last_error = f"network_url_error: {type(exc.reason).__name__}"
            cooldown = min(60.0, 5.0 * (2 ** min(self._consecutive_failures, 4)))
            self._schedule_backoff(cooldown)
            logger.warning(f"Dhan auth network error: {self._last_error} (cooldown {cooldown:.0f}s)")
            raise ValueError(f"dhan_auth_network_error: {self._last_error}") from exc

        except Exception as exc:
            self._state = DhanAuthState.DEGRADED
            self._last_error = f"auth_exception: {type(exc).__name__}"
            cooldown = min(60.0, 5.0 * (2 ** min(self._consecutive_failures, 4)))
            self._schedule_backoff(cooldown)
            logger.warning(f"Dhan auth unexpected exception: {self._last_error}")
            raise ValueError(f"dhan_auth_failure: {self._last_error}") from exc

    def _schedule_backoff(self, cooldown_seconds: float) -> None:
        self._consecutive_failures += 1
        self._cooldown_until_mono = time.monotonic() + cooldown_seconds

    def get_status(self) -> Dict[str, Any]:
        """
        Return sanitized observability dictionary without leaking any secrets or tokens.
        """
        return {
            "configured": self.is_configured(),
            "has_totp": self.has_totp_auth(),
            "state": self.state.value,
            "token_valid": self.is_token_valid(),
            "expires_in_sec": int(self.get_remaining_lifetime_sec()),
            "last_error": self._last_error,
            "generated_at": self._token_generated_at.isoformat() if self._token_generated_at else None,
            "expiry_time": self._token_expiry.isoformat() if self._token_expiry else None,
            "consecutive_failures": self._consecutive_failures,
        }


# Singleton Accessor
_GLOBAL_DHAN_TOKEN_MANAGER: Optional[DhanTokenManager] = None
_DHAN_MGR_LOCK = threading.Lock()


def get_dhan_token_manager() -> DhanTokenManager:
    """Return the global DhanTokenManager singleton."""
    global _GLOBAL_DHAN_TOKEN_MANAGER
    with _DHAN_MGR_LOCK:
        if _GLOBAL_DHAN_TOKEN_MANAGER is None:
            _GLOBAL_DHAN_TOKEN_MANAGER = DhanTokenManager()
        return _GLOBAL_DHAN_TOKEN_MANAGER

