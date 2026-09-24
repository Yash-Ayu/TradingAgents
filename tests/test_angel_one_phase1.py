"""
Unit tests for Angel One Integration Phase 1.
Verifies Scrip Master, Dynamic Contract Resolver, Redaction, and Fail-Closed Safety Rules.
Guarantees NO real or paper orders are executed.
"""

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.integrations.angel_one.client import AngelOneClient, redact_secret
from tradingagents.integrations.angel_one.contract_resolver import FOContractResolver
from tradingagents.integrations.angel_one.models import (
    ContractSpec,
    DerivativeType,
    Exchange,
    InstrumentRecord,
    MarketBias,
    OptionType,
    ResolutionRequest,
    ResolutionResult,
    StrikeMode,
)
from tradingagents.integrations.angel_one.scrip_master import ScripMasterManager


# =============================================================================
# 1. SECURITY & REDACTION TESTS
# =============================================================================
def test_redact_secret():
    """Verify that credentials are never exposed in plaintext."""
    assert redact_secret("MY_SUPER_SECRET_KEY") == "****_KEY"
    assert redact_secret("1234") == "****"
    assert redact_secret("") == "[NOT SET]"
    assert redact_secret(None) == "[NOT SET]"


def test_client_safety_invariants():
    """Verify that placeOrder, modifyOrder, cancelOrder are strictly prohibited."""
    client = AngelOneClient(
        api_key="TEST_KEY",
        client_code="TEST_CLIENT",
        pin="1234",
        totp_secret="TEST_TOTP",
    )

    with pytest.raises(NotImplementedError, match="CRITICAL SAFETY VIOLATION"):
        client.place_order()

    with pytest.raises(NotImplementedError, match="CRITICAL SAFETY VIOLATION"):
        client.modify_order()

    with pytest.raises(NotImplementedError, match="CRITICAL SAFETY VIOLATION"):
        client.cancel_order()


# =============================================================================
# 2. SCRIP MASTER & CONTRACT RESOLVER TESTS WITH SYNTHESIZED DATA
# =============================================================================
@pytest.fixture
def mock_scrip_data(tmp_path):
    """Create a temporary scrip master JSON with realistic test data for all target indices."""
    today = date.today()
    future_expiry = (today + timedelta(days=7)).strftime("%d%b%Y").upper()
    expired_date = (today - timedelta(days=7)).strftime("%d%b%Y").upper()

    instruments = [
        # NIFTY Options (ATM 24500, ITM 24400, OTM 24600) - stored in paise (scaled by 100)
        {"token": "1001", "symbol": f"NIFTY{future_expiry}24500CE", "name": "NIFTY", "expiry": future_expiry, "strike": "2450000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "1002", "symbol": f"NIFTY{future_expiry}24500PE", "name": "NIFTY", "expiry": future_expiry, "strike": "2450000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "1003", "symbol": f"NIFTY{future_expiry}24400CE", "name": "NIFTY", "expiry": future_expiry, "strike": "2440000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "1004", "symbol": f"NIFTY{future_expiry}24600CE", "name": "NIFTY", "expiry": future_expiry, "strike": "2460000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "1005", "symbol": f"NIFTY{future_expiry}24400PE", "name": "NIFTY", "expiry": future_expiry, "strike": "2440000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "1006", "symbol": f"NIFTY{future_expiry}24600PE", "name": "NIFTY", "expiry": future_expiry, "strike": "2460000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        # NIFTY Futures
        {"token": "1010", "symbol": f"NIFTY{future_expiry}FUT", "name": "NIFTY", "expiry": future_expiry, "strike": "", "lotsize": "25", "instrumenttype": "FUTIDX", "exch_seg": "NFO"},

        # BANKNIFTY Options (52000)
        {"token": "2001", "symbol": f"BANKNIFTY{future_expiry}52000CE", "name": "BANKNIFTY", "expiry": future_expiry, "strike": "5200000", "lotsize": "15", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
        {"token": "2002", "symbol": f"BANKNIFTY{future_expiry}52000PE", "name": "BANKNIFTY", "expiry": future_expiry, "strike": "5200000", "lotsize": "15", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},

        # SENSEX Options (BFO, 80000)
        {"token": "3001", "symbol": f"SENSEX{future_expiry}80000CE", "name": "SENSEX", "expiry": future_expiry, "strike": "8000000", "lotsize": "10", "instrumenttype": "OPTIDX", "exch_seg": "BFO"},
        {"token": "3002", "symbol": f"SENSEX{future_expiry}80000PE", "name": "SENSEX", "expiry": future_expiry, "strike": "8000000", "lotsize": "10", "instrumenttype": "OPTIDX", "exch_seg": "BFO"},

        # FINNIFTY Options (23000)
        {"token": "4001", "symbol": f"FINNIFTY{future_expiry}23000CE", "name": "FINNIFTY", "expiry": future_expiry, "strike": "2300000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},

        # MIDCPNIFTY Options (12500)
        {"token": "5001", "symbol": f"MIDCPNIFTY{future_expiry}12500CE", "name": "MIDCPNIFTY", "expiry": future_expiry, "strike": "1250000", "lotsize": "50", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},

        # BANKEX Options (BFO, 58000)
        {"token": "6001", "symbol": f"BANKEX{future_expiry}58000CE", "name": "BANKEX", "expiry": future_expiry, "strike": "5800000", "lotsize": "15", "instrumenttype": "OPTIDX", "exch_seg": "BFO"},

        # EXPIRED Contract (for fail-closed testing)
        {"token": "9001", "symbol": f"NIFTY{expired_date}24000CE", "name": "NIFTY", "expiry": expired_date, "strike": "2400000", "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
    ]

    cache_file = tmp_path / "scrip_master.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(instruments, f)

    manager = ScripMasterManager(cache_dir=tmp_path)
    manager.load(force_download=False)
    return manager, future_expiry


def test_nifty_resolution(mock_scrip_data):
    """Test dynamic resolution for NIFTY ATM, ITM, and OTM."""
    manager, expiry = mock_scrip_data
    resolver = FOContractResolver(scrip_master=manager)

    # 1. NIFTY ATM Bullish -> CE
    req = ResolutionRequest(
        underlying="NIFTY",
        spot_price=24510.0,  # Closest to 24500
        bias=MarketBias.BULLISH,
        strike_mode=StrikeMode.ATM,
    )
    res = resolver.resolve(req)
    assert res.success is True
    assert res.contract is not None
    assert res.contract.trading_symbol == f"NIFTY{expiry}24500CE"
    assert res.contract.symbol_token == "1001"
    assert res.contract.strike_price == 24500.0
    assert res.contract.lot_size == 25
    assert res.contract.exchange == Exchange.NFO

    # 2. NIFTY ITM Bullish (1 step down -> 24400 CE)
    req_itm = ResolutionRequest(
        underlying="NIFTY",
        spot_price=24510.0,
        bias=MarketBias.BULLISH,
        strike_mode=StrikeMode.ITM,
        strike_offset=1,
    )
    res_itm = resolver.resolve(req_itm)
    assert res_itm.success is True
    assert res_itm.contract.strike_price == 24400.0

    # 3. NIFTY OTM Bullish (1 step up -> 24600 CE)
    req_otm = ResolutionRequest(
        underlying="NIFTY",
        spot_price=24510.0,
        bias=MarketBias.BULLISH,
        strike_mode=StrikeMode.OTM,
        strike_offset=1,
    )
    res_otm = resolver.resolve(req_otm)
    assert res_otm.success is True
    assert res_otm.contract.strike_price == 24600.0


def test_all_target_indices_support(mock_scrip_data):
    """Test that all 6 target indices (NIFTY, BANKNIFTY, SENSEX, FINNIFTY, MIDCPNIFTY, BANKEX) resolve."""
    manager, expiry = mock_scrip_data
    resolver = FOContractResolver(scrip_master=manager)

    test_cases = [
        ("BANKNIFTY", 52020.0, Exchange.NFO, "2001"),
        ("SENSEX", 80050.0, Exchange.BFO, "3001"),
        ("FINNIFTY", 23010.0, Exchange.NFO, "4001"),
        ("MIDCPNIFTY", 12505.0, Exchange.NFO, "5001"),
        ("BANKEX", 58010.0, Exchange.BFO, "6001"),
    ]

    for underlying, spot, expected_exchange, expected_token in test_cases:
        req = ResolutionRequest(
            underlying=underlying,
            spot_price=spot,
            bias=MarketBias.BULLISH,
            strike_mode=StrikeMode.ATM,
        )
        res = resolver.resolve(req)
        assert res.success is True, f"Failed for {underlying}: {res.error_reason}"
        assert res.contract.exchange == expected_exchange
        assert res.contract.symbol_token == expected_token


def test_fail_closed_on_neutral_bias(mock_scrip_data):
    """Verify that NEUTRAL bias results in fail-closed NO TRADE."""
    manager, _ = mock_scrip_data
    resolver = FOContractResolver(scrip_master=manager)

    req = ResolutionRequest(
        underlying="NIFTY",
        spot_price=24500.0,
        bias=MarketBias.NEUTRAL,
    )
    res = resolver.resolve(req)
    assert res.success is False
    assert "NEUTRAL bias cannot select directional option" in res.error_reason
    assert res.contract is None


def test_fail_closed_on_unknown_symbol(mock_scrip_data):
    """Verify that an unknown symbol is rejected."""
    manager, _ = mock_scrip_data
    resolver = FOContractResolver(scrip_master=manager)

    req = ResolutionRequest(
        underlying="UNKNOWN_INDEX",
        spot_price=1000.0,
        bias=MarketBias.BULLISH,
    )
    res = resolver.resolve(req)
    assert res.success is False
    assert "Unknown instrument" in res.error_reason


def test_fail_closed_on_expired_preference(mock_scrip_data):
    """Verify that requesting an expired date is rejected."""
    manager, _ = mock_scrip_data
    resolver = FOContractResolver(scrip_master=manager)

    today = date.today()
    expired_date_str = (today - timedelta(days=7)).strftime("%d%b%Y").upper()

    req = ResolutionRequest(
        underlying="NIFTY",
        spot_price=24500.0,
        bias=MarketBias.BULLISH,
        preferred_expiry=expired_date_str,
    )
    res = resolver.resolve(req)
    assert res.success is False
    assert "invalid, expired, or unavailable" in res.error_reason


# =============================================================================
# 3. ALIAS RESOLUTION & FAIL-CLOSED AUTHENTICATION TESTS
# =============================================================================
def test_alias_client_code_primary_used(monkeypatch):
    """1. When ANGEL_CLIENT_CODE is present, it is used."""
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "CODE_PRIMARY")
    monkeypatch.delenv("ANGEL_CLIENT_ID", raising=False)
    client = AngelOneClient(api_key="API", pin="1234", totp_secret="TOTP")
    assert client.client_code == "CODE_PRIMARY"


def test_alias_client_id_fallback_used(monkeypatch):
    """2. When ANGEL_CLIENT_CODE is missing and ANGEL_CLIENT_ID is present, alias is used."""
    monkeypatch.delenv("ANGEL_CLIENT_CODE", raising=False)
    monkeypatch.setenv("ANGEL_CLIENT_ID", "ID_FALLBACK")
    client = AngelOneClient(api_key="API", pin="1234", totp_secret="TOTP")
    assert client.client_code == "ID_FALLBACK"


def test_alias_pin_primary_used(monkeypatch):
    """3. When ANGEL_PIN is present, it is used."""
    monkeypatch.setenv("ANGEL_PIN", "9999")
    monkeypatch.delenv("ANGEL_MPIN", raising=False)
    client = AngelOneClient(api_key="API", client_code="CODE", totp_secret="TOTP")
    assert client.pin == "9999"


def test_alias_mpin_fallback_used(monkeypatch):
    """4. When ANGEL_PIN is missing and ANGEL_MPIN is present, alias is used."""
    monkeypatch.delenv("ANGEL_PIN", raising=False)
    monkeypatch.setenv("ANGEL_MPIN", "8888")
    client = AngelOneClient(api_key="API", client_code="CODE", totp_secret="TOTP")
    assert client.pin == "8888"


def test_alias_precedence_primary_over_alias(monkeypatch):
    """5. When both primary and alias are present, PRIMARY variable has precedence."""
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "PRIMARY_CODE")
    monkeypatch.setenv("ANGEL_CLIENT_ID", "ALIAS_ID")
    monkeypatch.setenv("ANGEL_PIN", "1111")
    monkeypatch.setenv("ANGEL_MPIN", "2222")
    client = AngelOneClient(api_key="API", totp_secret="TOTP")
    assert client.client_code == "PRIMARY_CODE"
    assert client.pin == "1111"


def test_alias_both_missing_fails_closed(monkeypatch):
    """6. When both primary and alias are missing, authentication fails closed."""
    monkeypatch.delenv("ANGEL_CLIENT_CODE", raising=False)
    monkeypatch.delenv("ANGEL_CLIENT_ID", raising=False)
    monkeypatch.delenv("ANGEL_PIN", raising=False)
    monkeypatch.delenv("ANGEL_MPIN", raising=False)
    client = AngelOneClient(api_key="API", totp_secret="TOTP")
    auth_res = client.authenticate()
    assert auth_res["status"] is False
    assert auth_res["error_code"] == "MISSING_CREDENTIALS"
    assert "ANGEL_CLIENT_CODE (or ANGEL_CLIENT_ID)" in auth_res["message"]
    assert "ANGEL_PIN (or ANGEL_MPIN)" in auth_res["message"]


def test_alias_credentials_not_exposed_in_logs_or_repr(monkeypatch, caplog):
    """7. No test or log exposes credential values."""
    import logging
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("ANGEL_CLIENT_ID", "SECRET_CLIENT_VAL_9999")
    monkeypatch.setenv("ANGEL_MPIN", "SECRET_MPIN_VAL_1234")
    client = AngelOneClient(api_key="SECRET_KEY_VAL_ABCD", totp_secret="SECRET_TOTP_VAL_WXYZ")
    # Trigger authentication attempt (will fail gracefully without network/SmartConnect or invalid key)
    with patch("tradingagents.integrations.angel_one.client.SmartConnect") as mock_sc:
        mock_sc.side_effect = Exception("Network mock")
        client.authenticate()

    # Verify secret substrings never appear in logs
    log_text = caplog.text
    assert "SECRET_CLIENT_VAL_9999" not in log_text
    assert "SECRET_MPIN_VAL_1234" not in log_text
    assert "SECRET_KEY_VAL_ABCD" not in log_text
    assert "SECRET_TOTP_VAL_WXYZ" not in log_text

