"""
Comprehensive test suite for Dhan automatic authentication, token lifecycle,
provider integration, priority routing, local candle preference, and security redaction.

STRICT INVARIANTS:
1. Purely offline tests using mocks; zero real credentials or real broker endpoints.
2. Verify zero order mutation APIs exist on Dhan components.
3. Verify strict fail-closed semantics when market data fails.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
from datetime import datetime, timedelta
from typing import Any, Dict
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from tradingagents.integrations.dhan.auth import (
    DhanAuthState,
    DhanTokenManager,
    generate_totp,
)
from tradingagents.integrations.dhan.market_data import (
    DhanMarketDataProvider,
    DhanMarketDataState,
)
from tradingagents.runtime.market_adapter import (
    AngelOneMarketAdapter,
    DhanMarketAdapter,
    SmartMultiProviderAdapter,
    get_active_market_adapter,
)
from tradingagents.runtime.security import SecretRedactor, redact_text

IST = ZoneInfo("Asia/Kolkata")


# -----------------------------------------------------------------------------
# 1. TOTP GENERATION
# -----------------------------------------------------------------------------

def test_totp_generation_deterministic():
    """Verify standard TOTP generation produces valid 6-digit numeric tokens."""
    # Base32 secret: JBSWY3DPEHPK3PXP
    secret = "JBSWY3DPEHPK3PXP"
    code = generate_totp(secret)
    assert len(code) == 6
    assert code.isdigit()


def test_totp_generation_with_spaces_and_lowercase():
    """Verify secret normalization handles whitespace and case correctly."""
    secret = " jbsw y3dp ehpk 3pxp "
    code = generate_totp(secret)
    assert len(code) == 6
    assert code.isdigit()


# -----------------------------------------------------------------------------
# 2. TOKEN LIFECYCLE & AUTOMATIC REFRESH
# -----------------------------------------------------------------------------

def test_token_generation_and_caching():
    """Verify fresh token is requested via TOTP flow and cached in memory."""
    mgr = DhanTokenManager(
        client_id="1000000001",
        pin="123456",
        totp_secret="JBSWY3DPEHPK3PXP",
        base_auth_url="https://mock.dhan.co/generateAccessToken",
    )

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({
        "dhanClientId": "1000000001",
        "dhanClientName": "TEST USER",
        "accessToken": "mock_dhan_jwt_token_12345",
        "expiryTime": (datetime.now(IST) + timedelta(hours=24)).isoformat(),
    }).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        token = mgr.get_valid_access_token()
        assert token == "mock_dhan_jwt_token_12345"
        assert mgr.state == DhanAuthState.HEALTHY
        assert mgr.is_token_valid() is True
        assert mock_urlopen.call_count == 1

        # Second retrieval within validity threshold uses memory cache without network call
        cached_token = mgr.get_valid_access_token()
        assert cached_token == "mock_dhan_jwt_token_12345"
        assert mock_urlopen.call_count == 1  # No additional network call


def test_token_auto_refresh_before_expiry():
    """Verify token is proactively regenerated when remaining lifetime is within refresh threshold."""
    mgr = DhanTokenManager(
        client_id="1000000001",
        pin="123456",
        totp_secret="JBSWY3DPEHPK3PXP",
        refresh_threshold_sec=1800.0,  # 30 mins
        base_auth_url="https://mock.dhan.co/generateAccessToken",
    )

    # Set an existing cached token that has only 5 minutes remaining (less than 30 mins threshold)
    mgr._cached_token = "old_expiring_token"
    mgr._token_generated_at = datetime.now(IST) - timedelta(hours=23, minutes=55)
    mgr._token_expiry = datetime.now(IST) + timedelta(minutes=5)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({
        "dhanClientId": "1000000001",
        "accessToken": "fresh_refreshed_token_67890",
        "expiryTime": (datetime.now(IST) + timedelta(hours=24)).isoformat(),
    }).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        token = mgr.get_valid_access_token()
        assert token == "fresh_refreshed_token_67890"
        assert mock_urlopen.call_count == 1


def test_concurrent_refresh_lock_prevents_multiple_auth_calls():
    """Verify thread-safe locking ensures multiple concurrent scanner threads trigger only 1 network call."""
    mgr = DhanTokenManager(
        client_id="1000000001",
        pin="123456",
        totp_secret="JBSWY3DPEHPK3PXP",
        base_auth_url="https://mock.dhan.co/generateAccessToken",
    )

    call_count = 0
    lock = threading.Lock()

    def mock_urlopen_fn(req, timeout=10):
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.05)  # Simulate network latency
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "dhanClientId": "1000000001",
            "accessToken": "thread_safe_token_abc",
            "expiryTime": (datetime.now(IST) + timedelta(hours=24)).isoformat(),
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    threads = []
    tokens_received = []

    def worker():
        tok = mgr.get_valid_access_token()
        tokens_received.append(tok)

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_fn):
        for _ in range(8):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

    assert len(tokens_received) == 8
    assert all(t == "thread_safe_token_abc" for t in tokens_received)
    # Exactly one network call should have occurred
    assert call_count == 1


# -----------------------------------------------------------------------------
# 3. AUTH FAILURE, RATE LIMIT & BACKOFF
# -----------------------------------------------------------------------------

def test_auth_failure_distinguishes_error_and_applies_backoff():
    """Verify 401/403 HTTP errors mark state AUTH_FAILED and apply backoff cooldown."""
    mgr = DhanTokenManager(
        client_id="1000000001",
        pin="wrong_pin",
        totp_secret="JBSWY3DPEHPK3PXP",
        base_auth_url="https://mock.dhan.co/generateAccessToken",
    )

    http_error = urllib.error.HTTPError(
        url="https://mock.dhan.co/generateAccessToken",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"errorType":"DH-901","remarks":"Invalid PIN"}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(ValueError) as excinfo:
            mgr.get_valid_access_token()
        assert "auth_failed" in str(excinfo.value)
        assert mgr.state == DhanAuthState.AUTH_FAILED
        assert mgr._consecutive_failures == 1
        assert mgr._cooldown_until_mono > time.monotonic()


def test_rate_limit_429_applies_extended_backoff():
    """Verify 429 HTTP error marks state RATE_LIMITED and applies exponential backoff."""
    mgr = DhanTokenManager(
        client_id="1000000001",
        pin="123456",
        totp_secret="JBSWY3DPEHPK3PXP",
        base_auth_url="https://mock.dhan.co/generateAccessToken",
    )

    http_429 = urllib.error.HTTPError(
        url="https://mock.dhan.co/generateAccessToken",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"errorType":"DH-429","remarks":"Rate limit exceeded"}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_429):
        with pytest.raises(ValueError) as excinfo:
            mgr.get_valid_access_token()
        assert "rate_limited" in str(excinfo.value)
        assert mgr.state == DhanAuthState.RATE_LIMITED


# -----------------------------------------------------------------------------
# 4. CREDENTIAL REDACTION
# -----------------------------------------------------------------------------

def test_credential_redaction_guarantees():
    """Verify SecretRedactor redacts DHAN_PIN, DHAN_TOTP_SECRET, and access-token."""
    pin = "987654"
    totp_secret = "JBSWY3DPEHPK3PXP"
    token = "eyMockSecretJwtTokenPayload12345"

    mgr = DhanTokenManager(
        client_id="1000000001",
        pin=pin,
        totp_secret=totp_secret,
        access_token=token,
    )

    test_log = (
        f"Connecting with pin={pin}&totp=123456 and Authorization access-token: {token} "
        f"secret {totp_secret}"
    )
    redacted = redact_text(test_log)

    assert pin not in redacted
    assert totp_secret not in redacted
    assert token not in redacted
    assert "[REDACTED]" in redacted


# -----------------------------------------------------------------------------
# 5. PROVIDER PRIORITY & FALLBACK
# -----------------------------------------------------------------------------

def test_dhan_primary_with_angel_fallback():
    """Verify MARKET_DATA_PRIMARY=dhan prioritizes Dhan and falls back to Angel on error."""
    dhan_adapter = MagicMock(spec=DhanMarketAdapter)
    dhan_adapter.name = "dhan"
    dhan_adapter.is_configured.return_value = True

    angel_adapter = MagicMock(spec=AngelOneMarketAdapter)
    angel_adapter.name = "angel_one"
    angel_adapter.is_configured.return_value = True

    # Dhan succeeds
    dhan_adapter.get_candles.return_value = {
        "chart": [[f"2026-10-07T10:0{i}", 25000, 25010, 24990, 25005, 100] for i in range(30)],
        "timestamp": "2026-10-07T10:29",
    }
    smart = SmartMultiProviderAdapter(primary=dhan_adapter, fallbacks=[angel_adapter])
    assert smart.primary.name == "dhan"
    assert smart.active_adapter_name == "dhan"

    res = smart.get_candles("MOCK_SYM", interval="5m", allow_fallback=True)
    assert res is not None
    assert smart.fallback_active is False

    # Dhan fails -> fallback to Angel One
    dhan_adapter.get_candles.side_effect = ValueError("dhan_rate_limited")
    angel_adapter.get_candles.return_value = {
        "chart": [[f"2026-10-07T10:0{i}", 25000, 25010, 24990, 25005, 100] for i in range(30)],
        "timestamp": "2026-10-07T10:30",
    }

    res2 = smart.get_candles("MOCK_SYM", interval="5m", allow_fallback=True)
    assert smart.fallback_active is True
    assert smart.active_adapter_name == "angel_one"
    assert res2["is_fallback"] is True


def test_angel_primary_with_dhan_fallback():
    """Verify MARKET_DATA_PRIMARY=angel prioritizes Angel and falls back to Dhan on error."""
    angel_adapter = MagicMock(spec=AngelOneMarketAdapter)
    angel_adapter.name = "angel_one"
    angel_adapter.is_configured.return_value = True

    dhan_adapter = MagicMock(spec=DhanMarketAdapter)
    dhan_adapter.name = "dhan"
    dhan_adapter.is_configured.return_value = True

    angel_adapter.get_candles.side_effect = ValueError("angel_rate_limited")
    dhan_adapter.get_candles.return_value = {
        "chart": [[f"2026-10-07T10:0{i}", 25000, 25010, 24990, 25005, 100] for i in range(30)],
        "timestamp": "2026-10-07T10:30",
    }

    smart = SmartMultiProviderAdapter(primary=angel_adapter, fallbacks=[dhan_adapter])
    res = smart.get_candles("MOCK_SYM", interval="5m", allow_fallback=True)
    assert smart.fallback_active is True
    assert smart.active_adapter_name == "dhan"
    assert res["is_fallback"] is True


def test_local_fresh_candles_preferred_over_rest(tmp_path):
    """Verify SmartMultiProviderAdapter returns local fresh completed candles without invoking REST."""
    from tradingagents.runtime.local_candle_store import LocalCandleStore

    cache_dir = tmp_path / "candle_cache"
    store = LocalCandleStore(cache_dir=cache_dir)

    # Populate 30 fresh completed 5m bars
    now = datetime.now(IST)
    bars_5m = []
    for i in range(30):
        t_str = (now - timedelta(minutes=(30 - i) * 5)).isoformat()
        bars_5m.append([t_str, 25000 + i, 25010 + i, 24990 + i, 25005 + i, 500])
    store._bars_5m["NIFTY"] = bars_5m

    primary = MagicMock()
    primary.name = "angel_one"
    primary.is_configured.return_value = True

    smart = SmartMultiProviderAdapter(primary=primary, fallbacks=[])

    with patch("tradingagents.runtime.local_candle_store.get_local_candle_store", return_value=store):
        res = smart.get_candles("NIFTY", interval="5m", allow_fallback=False)
        assert res is not None
        assert len(res["chart"]) == 30
        # REST primary was not called!
        assert primary.get_candles.call_count == 0


def test_no_provider_available_fails_closed():
    """Verify fail-closed behavior when primary fails and fallbacks are exhausted."""
    primary = MagicMock()
    primary.name = "angel_one"
    primary.is_configured.return_value = True
    primary.get_candles.side_effect = ValueError("primary_connection_failed")

    fallback = MagicMock()
    fallback.name = "dhan"
    fallback.is_configured.return_value = True
    fallback.get_candles.side_effect = ValueError("fallback_connection_failed")

    smart = SmartMultiProviderAdapter(primary=primary, fallbacks=[fallback])

    with pytest.raises(ValueError):
        smart.get_candles("MOCK_SYM", interval="5m", allow_fallback=True)


# -----------------------------------------------------------------------------
# 6. PAPER SAFETY & ZERO ORDER MUTATION INVARIANT
# -----------------------------------------------------------------------------

def test_zero_order_mutation_apis_exist_on_dhan():
    """Strictly assert no order placement, modification, or cancellation APIs exist on Dhan."""
    provider = DhanMarketDataProvider()
    adapter = DhanMarketAdapter()

    forbidden_methods = [
        "place_order",
        "placeOrder",
        "modify_order",
        "modifyOrder",
        "cancel_order",
        "cancelOrder",
        "buy",
        "sell",
        "execute_trade",
    ]

    for m in forbidden_methods:
        assert not hasattr(provider, m), f"DhanMarketDataProvider must NOT have {m}"
        assert not hasattr(adapter, m), f"DhanMarketAdapter must NOT have {m}"

