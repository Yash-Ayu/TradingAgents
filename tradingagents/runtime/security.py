"""Security, secret redaction, and error classification for Angel One and runtime operations.

Guarantees:
- Zero credential leakage across Python logging, console output, exceptions, telemetry, and ledger.
- Intercepts SmartAPI SDK logzero.logger to redact Authorization headers, JWT tokens, and API keys.
- Maps raw network/provider exceptions into safe, deterministic reason codes.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from typing import Any

logger = logging.getLogger(__name__)

# Common header/credential patterns for regex redaction
SENSITIVE_PATTERNS = [
    # Authorization: Bearer <token>
    re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]+', re.IGNORECASE),
    # Headers JSON/dict representation: "Authorization": "..."
    re.compile(r'("Authorization"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('Authorization'\s*:\s*')[^']+(')", re.IGNORECASE),
    # X-PrivateKey
    re.compile(r'("X-PrivateKey"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('X-PrivateKey'\s*:\s*')[^']+(')", re.IGNORECASE),
    # jwtToken
    re.compile(r'("jwtToken"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('jwtToken'\s*:\s*')[^']+(')", re.IGNORECASE),
    # refreshToken
    re.compile(r'("refreshToken"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('refreshToken'\s*:\s*')[^']+(')", re.IGNORECASE),
    # feedToken
    re.compile(r'("feedToken"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('feedToken'\s*:\s*')[^']+(')", re.IGNORECASE),
    # password / mpin / totp / secret / api_key in JSON/dict dumps
    re.compile(r'("password"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('password'\s*:\s*')[^']+(')", re.IGNORECASE),
    re.compile(r'("mpin"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('mpin'\s*:\s*')[^']+(')", re.IGNORECASE),
    re.compile(r'("totp"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('totp'\s*:\s*')[^']+(')", re.IGNORECASE),
    re.compile(r'("api_key"\s*:\s*")[^"]+(")', re.IGNORECASE),
    re.compile(r"('api_key'\s*:\s*')[^']+(')", re.IGNORECASE),
    # SmartConnect full Headers dump: Headers: {'Authorization': ...}
    re.compile(r'Headers:\s*\{.*?\}', re.DOTALL),
]


class SecretRedactor:
    """Singleton registry and redactor for runtime secrets and credentials."""

    _instance: SecretRedactor | None = None

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._init_from_env()

    @classmethod
    def get_instance(cls) -> SecretRedactor:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_from_env(self) -> None:
        """Register known credential environment variable values."""
        for var in (
            "ANGEL_API_KEY",
            "ANGEL_CLIENT_ID",
            "ANGEL_CLIENT_CODE",
            "ANGEL_MPIN",
            "ANGEL_PIN",
            "ANGEL_TOTP_SECRET",
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
        ):
            val = os.environ.get(var)
            if val and len(val.strip()) >= 3:
                self.register(val.strip())

    def register(self, secret: str | None) -> None:
        """Register a secret string to be redacted whenever logged or formatted."""
        if secret and isinstance(secret, str):
            s = secret.strip()
            if len(s) >= 4:
                self._secrets.add(s)

    def redact(self, text: Any) -> str:
        """Replace all occurrences of registered secrets and sensitive patterns with [REDACTED]."""
        if text is None:
            return ""
        s = str(text)

        # 1. Exact string matches for registered secrets
        for sec in sorted(self._secrets, key=len, reverse=True):
            if sec in s:
                s = s.replace(sec, "[REDACTED]")

        # 2. Pattern-based redaction for tokens, Bearer auth, and headers
        for pattern in SENSITIVE_PATTERNS:
            s = pattern.sub(
                lambda m: "[REDACTED]" if len(m.groups()) == 0 else f"{m.group(1)}[REDACTED]{m.group(2) if len(m.groups()) > 1 else ''}",
                s,
            )

        return s


def register_secret(val: str | None) -> None:
    """Convenience helper to register a secret with the global redactor."""
    SecretRedactor.get_instance().register(val)


def redact_text(val: Any) -> str:
    """Convenience helper to redact secrets from text."""
    return SecretRedactor.get_instance().redact(val)


class SecretRedactingLoggingFilter(logging.Filter):
    """Logging filter that sanitizes records before they are formatted or written."""

    def filter(self, record: logging.LogRecord) -> bool:
        redactor = SecretRedactor.get_instance()
        if record.msg and isinstance(record.msg, str):
            record.msg = redactor.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redactor.redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redactor.redact(a) if isinstance(a, str) else a for a in record.args)
        if record.exc_info:
            try:
                formatted = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = redactor.redact(formatted)
                record.exc_info = None
            except Exception:
                pass
        elif getattr(record, "exc_text", None):
            record.exc_text = redactor.redact(record.exc_text)
        return True


_REDACTION_INSTALLED = False


def install_secret_redaction() -> None:
    """Installs the SecretRedactingLoggingFilter on logzero and standard library loggers."""
    global _REDACTION_INSTALLED
    if _REDACTION_INSTALLED:
        return

    redacting_filter = SecretRedactingLoggingFilter()

    # 1. Root logger and its handlers
    root = logging.getLogger()
    root.addFilter(redacting_filter)
    for h in root.handlers:
        h.addFilter(redacting_filter)

    # 2. SmartApi / urllib3 / requests loggers
    for name in ("SmartApi", "smartapi", "urllib3", "requests", "tradingagents"):
        log_instance = logging.getLogger(name)
        log_instance.addFilter(redacting_filter)
        for h in log_instance.handlers:
            h.addFilter(redacting_filter)

    # 3. logzero logger (used directly by SmartConnect)
    if "logzero" in sys.modules:
        try:
            import logzero
            logzero.logger.addFilter(redacting_filter)
            for h in logzero.logger.handlers:
                h.addFilter(redacting_filter)
        except Exception:
            pass

    _REDACTION_INSTALLED = True


# Initialize redaction upon module import
install_secret_redaction()


# =============================================================================
# ERROR CLASSIFICATION FOR ANGEL ONE CALLS
# =============================================================================

def is_rate_limit(exc_or_resp: Any) -> bool:
    """Checks whether an exception or response indicates an Angel One rate-limit event."""
    if exc_or_resp is None:
        return False
    if isinstance(exc_or_resp, dict):
        code = str(exc_or_resp.get("errorcode", "")).upper()
        msg = str(exc_or_resp.get("message", "")).lower()
        return code in {"AB1004", "429"} or "exceeding access rate" in msg or "rate limit" in msg

    msg = str(exc_or_resp).lower()
    return (
        "exceeding access rate" in msg
        or "rate limit" in msg
        or "too many requests" in msg
        or "429" in msg
        or "ab1004" in msg
    )


def extract_rate_limit_marker(exc_or_resp: Any) -> str:
    """Extracts only the safe recognized rate-limit marker without leaking payload or request text."""
    if exc_or_resp is None:
        return "unknown"
    if isinstance(exc_or_resp, dict):
        code = str(exc_or_resp.get("errorcode", "")).upper()
        if code == "AB1004":
            return "AB1004"
        if code == "429":
            return "429"
        msg = str(exc_or_resp.get("message", "")).lower()
    else:
        msg = str(exc_or_resp).lower()

    if "ab1004" in msg:
        return "AB1004"
    if "429" in msg:
        return "429"
    if "exceeding access rate" in msg:
        return "exceeding access rate"
    if "too many requests" in msg:
        return "too many requests"
    if "rate limit" in msg:
        return "rate limit"
    return "unknown"


def is_auth_error(exc_or_resp: Any) -> bool:
    """Checks whether an exception or response indicates an expired session or auth failure."""
    if exc_or_resp is None:
        return False
    if isinstance(exc_or_resp, dict):
        code = str(exc_or_resp.get("errorcode", "")).upper()
        msg = str(exc_or_resp.get("message", "")).lower()
        if code in {"AG8001", "AB1000", "AB1001", "AB2001", "401"}:
            return True
        return any(term in msg for term in ("invalid token", "token is expired", "session expired", "unauthorized"))

    msg = str(exc_or_resp).lower()
    return any(
        term in msg
        for term in (
            "invalid token",
            "token is expired",
            "session expired",
            "unauthorized",
            "invalid jwt",
            "jwt expired",
            "ag8001",
            "ab1000",
            "ab1001",
        )
    )


def is_transient_network_error(exc: Any) -> bool:
    """Checks whether an exception is a transient network or connection drop."""
    if exc is None:
        return False
    if isinstance(exc, (ConnectionResetError, ConnectionError, ConnectionRefusedError, TimeoutError)):
        return True

    exc_type = type(exc).__name__
    if exc_type in (
        "ConnectionError",
        "ConnectionResetError",
        "Timeout",
        "ReadTimeout",
        "ConnectTimeout",
        "ProtocolError",
        "RemoteDisconnected",
    ):
        return True

    msg = str(exc).lower()
    return any(
        term in msg
        for term in (
            "forcibly closed",
            "remotedisconnected",
            "connection reset",
            "connection refused",
            "timed out",
            "timeout",
            "network is unreachable",
            "broken pipe",
        )
    )


def classify_angel_error(exc_or_resp: Any) -> str:
    """Classifies an Angel error or exception into a safe, deterministic reason code."""
    if is_rate_limit(exc_or_resp):
        return "angel_rate_limited"
    if is_auth_error(exc_or_resp):
        return "angel_auth_failed"
    if is_transient_network_error(exc_or_resp):
        msg = str(exc_or_resp).lower()
        if "timeout" in msg or "timed out" in msg:
            return "angel_timeout"
        return "angel_connection_error"
    return "angel_data_unavailable"
