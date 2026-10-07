"""Comprehensive reliability, retry, secret redaction, and paper safety tests for Angel One.

Verifies:
1. First read request ConnectionError -> bounded retry succeeds.
2. Repeated ConnectionError -> stops after max attempts and fails closed with angel_connection_error.
3. Timeout -> bounded retry, fails closed with angel_timeout.
4. Rate-limit response -> classified as angel_rate_limited, no tight-loop retry, respects cooldown.
5. Invalid/expired session -> triggers controlled re-authentication (1 attempt).
6. Re-auth failure -> fails closed with angel_auth_failed and resets client.
7. Secret redaction -> fake Authorization token, API key, MPIN, TOTP never appear in captured logs/output/errors.
8. No real broker mutation API invoked.
9. placeOrder, modifyOrder, cancelOrder remain hard-blocked on read-only feed.
10. Freshness and provenance gates unchanged (LIVE_ANGEL_ONE required for trade).
11. Per-symbol scanner failure does not crash complete universe scan.
12. Existing scanner batch rotation still works.
13. Reference INDEX validation regression remains green.
14. live_execution remains false.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.runtime.angel_data import AngelReadOnlyFeed
from tradingagents.runtime.market_snapshot import (
    IST,
    Instrument,
    SessionCalendar,
)
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService
from tradingagents.runtime.security import (
    redact_text,
    register_secret,
)


def _sample_candles(now: datetime, count: int = 35):
    minute = now.replace(second=0, microsecond=0)
    rows = []
    for i in range(count):
        stamp = minute - timedelta(minutes=count - i)
        rows.append([stamp.isoformat(), 100.0 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1, 100.2 + i * 0.1, 5000])
    return rows


def _valid_quotes(instrument: Instrument, vix_instrument: Instrument, now: datetime):
    return {
        'status': True,
        'data': {
            'fetched': [
                {
                    'symbolToken': instrument.token,
                    'exchange': instrument.exchange,
                    'ltp': 100.0,
                    'exchFeedTime': now.isoformat(),
                },
                {
                    'symbolToken': vix_instrument.token,
                    'exchange': vix_instrument.exchange,
                    'ltp': 14.5,
                    'exchFeedTime': now.isoformat(),
                },
            ],
            'unfetched': [],
        },
    }


@pytest.fixture
def test_clock():
    return [datetime(2026, 9, 29, 10, 30, 0, tzinfo=IST)]


@pytest.fixture
def instruments():
    inst = Instrument('NSE', 'TEST-EQ', '123', 1, 'EQ')
    vix = Instrument('NSE', 'India VIX', '456', 1, 'INDEX')
    return inst, vix


@pytest.fixture
def mock_client(instruments, test_clock):
    inst, vix = instruments
    client = MagicMock()
    now = test_clock[0]
    client.getMarketData.return_value = _valid_quotes(inst, vix, now)
    client.getCandleData.return_value = {'status': True, 'data': _sample_candles(now)}
    client.rmsLimit.return_value = {'status': True, 'data': {'availablecash': '500000.0'}}
    client.position.return_value = {'status': True, 'data': []}
    client.orderBook.return_value = {'status': True, 'data': []}
    return client


# =============================================================================
# 1. RETRY ON TRANSIENT NETWORK ERROR
# =============================================================================

def test_1_retry_on_connection_reset_succeeds(instruments, mock_client, test_clock):
    """TEST 1: First read request ConnectionError -> bounded retry succeeds."""
    inst, vix = instruments
    now = test_clock[0]
    feed = AngelReadOnlyFeed(inst, vix)
    feed.client = mock_client
    feed.connected = True

    # First call raises ConnectionResetError, second call returns valid quotes
    mock_client.getMarketData.side_effect = [
        ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"),
        _valid_quotes(inst, vix, now),
    ]

    snapshot = feed.fetch(now)
    assert snapshot['price'] == 100.0
    assert snapshot['vix'] == 14.5
    assert mock_client.getMarketData.call_count == 2
    assert feed.connected is True


def test_2_repeated_connection_error_fails_closed(instruments, mock_client, test_clock):
    """TEST 2: Repeated ConnectionError -> stops after max attempts and fails closed with angel_connection_error."""
    inst, vix = instruments
    now = test_clock[0]
    feed = AngelReadOnlyFeed(inst, vix)
    feed.client = mock_client
    feed.connected = True

    mock_client.getMarketData.side_effect = ConnectionResetError(
        10054, "An existing connection was forcibly closed by the remote host"
    )

    with pytest.raises(ValueError, match="angel_connection_error"):
        feed.fetch(now)

    # 1 initial attempt + 2 retries = 3 attempts total
    assert mock_client.getMarketData.call_count == 3
    # Transient connection drops do NOT wipe the client/session
    assert feed.client is not None


def test_3_timeout_fails_closed_with_angel_timeout(instruments, mock_client, test_clock):
    """TEST 3: Timeout -> bounded retry, fails closed with angel_timeout."""
    inst, vix = instruments
    now = test_clock[0]
    feed = AngelReadOnlyFeed(inst, vix)
    feed.client = mock_client
    feed.connected = True

    mock_client.getMarketData.side_effect = TimeoutError("Request timed out")

    with pytest.raises(ValueError, match="angel_timeout"):
        feed.fetch(now)

    assert mock_client.getMarketData.call_count == 3


# =============================================================================
# 4. RATE LIMIT HANDLING AND COOLDOWN
# =============================================================================

def test_4_rate_limit_cooldown_and_no_tight_loop(instruments, mock_client, test_clock):
    """TEST 4: Rate-limit response -> classified as angel_rate_limited, no tight loop, respects cooldown."""
    inst, vix = instruments
    now = test_clock[0]
    feed = AngelReadOnlyFeed(inst, vix)
    feed.client = mock_client
    feed.connected = True

    # Angel One rate-limit error response
    mock_client.getMarketData.return_value = {
        'status': False,
        'message': 'Access denied because of exceeding access rate',
        'errorcode': 'AB1004',
    }

    # First call triggers rate limit
    with pytest.raises(ValueError, match="angel_rate_limited"):
        feed.fetch(now)

    assert mock_client.getMarketData.call_count == 1

    # Second immediate call within 3s cooldown must raise immediately without calling client
    with pytest.raises(ValueError, match="angel_rate_limited"):
        feed.fetch(now)

    # Call count should still be 1 (client was NOT hit during cooldown)
    assert mock_client.getMarketData.call_count == 1


# =============================================================================
# 5 & 6. CONTROLLED AUTH RECOVERY
# =============================================================================

def test_5_invalid_session_triggers_controlled_reauth(instruments, mock_client, test_clock):
    """TEST 5: Invalid/expired session -> triggers controlled re-authentication (1 attempt)."""
    inst, vix = instruments
    now = test_clock[0]
    feed = AngelReadOnlyFeed(inst, vix)
    feed.client = mock_client
    feed.connected = True

    # First call returns token expired, second returns valid quotes
    mock_client.getMarketData.side_effect = [
        {'status': False, 'message': 'Invalid Token', 'errorcode': 'AG8001'},
        _valid_quotes(inst, vix, now),
    ]

    reauth_called = False

    def fake_connect(force=False):
        nonlocal reauth_called
        reauth_called = True
        feed.connected = True

    feed.connect = MagicMock(side_effect=fake_connect)

    snapshot = feed.fetch(now)
    assert snapshot['price'] == 100.0
    assert reauth_called is True
    assert mock_client.getMarketData.call_count == 2


def test_6_reauth_failure_fails_closed(instruments, mock_client, test_clock):
    """TEST 6: Re-auth failure -> fails closed with angel_auth_failed and resets client."""
    inst, vix = instruments
    now = test_clock[0]
    feed = AngelReadOnlyFeed(inst, vix)
    feed.client = mock_client
    feed.connected = True

    mock_client.getMarketData.return_value = {
        'status': False,
        'message': 'Invalid Token',
        'errorcode': 'AG8001',
    }

    def failing_connect(force=False):
        raise ValueError('angel_auth_failed')

    feed.connect = MagicMock(side_effect=failing_connect)

    with pytest.raises(ValueError, match="angel_auth_failed"):
        feed.fetch(now)

    # Client and connection state reset on unrecoverable auth failure
    assert feed.client is None
    assert feed.connected is False


# =============================================================================
# 7. SECRET REDACTION TESTS
# =============================================================================

def test_7_secret_redaction_in_logs_and_exceptions():
    """TEST 7: Secret redaction -> fake tokens, API keys, MPINs never leak into logs or errors."""
    fake_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.s64w3Pp8szX.FAKE_JWT_FOR_TEST_PURPOSE"
    fake_api_key = "FAKE_API_KEY_SECRET_987654321"
    fake_mpin = "9876_SECRET_PIN"

    register_secret(fake_token)
    register_secret(fake_api_key)
    register_secret(fake_mpin)

    # Direct text redaction
    sample_text = f"Failed call with key={fake_api_key} and token={fake_token} mpin={fake_mpin}"
    redacted = redact_text(sample_text)
    assert fake_token not in redacted
    assert fake_api_key not in redacted
    assert fake_mpin not in redacted
    assert "[REDACTED]" in redacted

    # Pattern redaction: Bearer header
    bearer_text = f"Authorization: Bearer {fake_token}"
    assert fake_token not in redact_text(bearer_text)

    # Pattern redaction: SmartConnect dump representation
    header_dump = f"Headers: {{'Authorization': 'Bearer {fake_token}', 'X-PrivateKey': '{fake_api_key}'}}"
    redacted_dump = redact_text(header_dump)
    assert fake_token not in redacted_dump
    assert fake_api_key not in redacted_dump

    # Logger test
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    test_logger = logging.getLogger("tradingagents.security_test")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)

    from tradingagents.runtime.security import SecretRedactingLoggingFilter
    handler.addFilter(SecretRedactingLoggingFilter())

    test_logger.info(f"API key leaked in log: {fake_api_key}")
    output = log_capture.getvalue()
    assert fake_api_key not in output
    assert "[REDACTED]" in output


# =============================================================================
# 8 & 9. BROKER MUTATION APIs PERMANENTLY BLOCKED
# =============================================================================

def test_8_no_real_broker_mutation_api_invoked(instruments, mock_client, tmp_path, test_clock):
    """TEST 8: Ensure real broker order APIs are never called during paper cycles."""
    inst, vix = instruments
    feed = AngelReadOnlyFeed(inst, vix)
    feed.client = mock_client
    feed.connected = True

    ledger = PaperLedger(tmp_path / "paper_test_safety.sqlite3", initial_cash=100000.0)
    service = PaperTradingService(
        feed,
        ledger,
        calendar=SessionCalendar({'year': 2026}),
        clock=lambda: test_clock[0],
    )

    service.start(background=False)
    # Tick the service
    result = service.tick()
    # Confirm result is a paper action or monitoring
    assert result['status'] in ('monitoring', 'risk_blocked', 'filled')
    # Mock client must NOT have any order execution calls
    assert not hasattr(mock_client, 'placeOrder') or mock_client.placeOrder.call_count == 0
    assert not hasattr(mock_client, 'modifyOrder') or mock_client.modifyOrder.call_count == 0
    assert not hasattr(mock_client, 'cancelOrder') or mock_client.cancelOrder.call_count == 0
    ledger.close()


def test_9_place_modify_cancel_order_hard_blocked(instruments):
    """TEST 9: AngelReadOnlyFeed order modification methods are hard-blocked."""
    inst, vix = instruments
    feed = AngelReadOnlyFeed(inst, vix)

    with pytest.raises(PermissionError, match="broker_order_mutation_permanently_blocked"):
        feed.placeOrder()

    with pytest.raises(PermissionError, match="broker_order_mutation_permanently_blocked"):
        feed.modifyOrder()

    with pytest.raises(PermissionError, match="broker_order_mutation_permanently_blocked"):
        feed.cancelOrder()


# =============================================================================
# 10. PROVENANCE GATE REMAINS INTACT
# =============================================================================

def test_10_provenance_and_freshness_gates_unchanged(tmp_path, test_clock):
    """TEST 10: Freshness and provenance gates unchanged (LIVE_ANGEL_ONE required for trade)."""
    inst = Instrument('NSE', 'Nifty 50', '99926000', 1, 'INDEX')
    vix = Instrument('NSE', 'India VIX', '456', 1, 'INDEX')
    feed = AngelReadOnlyFeed(inst, vix)
    feed.connected = True

    ledger = PaperLedger(tmp_path / "paper_test_prov.sqlite3", initial_cash=100000.0)
    service = PaperTradingService(
        feed,
        ledger,
        calendar=SessionCalendar({'year': 2026}),
        clock=lambda: test_clock[0],
        scanner_batch_size=4,
    )

    now = test_clock[0]
    bar_start = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).isoformat()
    snapshot = {
        'source': 'angel',
        'instrument': inst.as_dict(),
        'price': 25000.0,
        'vix': 14.0,
        'atr': 50.0,
        'atr_ratio': 1.0,
        'trend_strength': 0.8,
        'timestamp': now.isoformat(),
        'vix_timestamp': now.isoformat(),
        'bar_timestamp': bar_start,
        'chart': _sample_candles(now),
        'broker_account': {'available_cash': 500000.0, 'orders': []},
    }

    # Mock market adapter to return non-LIVE_ANGEL_ONE data
    fake_adapter = MagicMock()
    fake_adapter.get_candles.return_value = {
        'source': 'RESEARCH_FALLBACK',
        'chart': _sample_candles(now),
    }

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=fake_adapter):
        service._scan_and_execute_fo(snapshot, {'cash': 500000.0}, 1)

    # In angel mode, non-LIVE_ANGEL_ONE source must be rejected by provenance gate
    last_scan = service.last_scanner_result
    assert last_scan is not None
    assert any('provenance_rejected_RESEARCH_FALLBACK' in k for k in last_scan.get('rejected_by_reason', {}))
    ledger.close()


# =============================================================================
# 11. PER-SYMBOL SCANNER FAILURE ISOLATION
# =============================================================================

def test_11_per_symbol_scanner_failure_isolation(tmp_path, test_clock):
    """TEST 11: Per-symbol scanner failure does not crash complete universe scan."""
    inst = Instrument('NSE', 'Nifty 50', '99926000', 1, 'INDEX')
    vix = Instrument('NSE', 'India VIX', '456', 1, 'INDEX')
    feed = AngelReadOnlyFeed(inst, vix)
    feed.connected = True

    ledger = PaperLedger(tmp_path / "paper_test_isolation.sqlite3", initial_cash=100000.0)
    service = PaperTradingService(
        feed,
        ledger,
        calendar=SessionCalendar({'year': 2026}),
        clock=lambda: test_clock[0],
        scanner_batch_size=4,
    )

    now = test_clock[0]
    bar_start = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).isoformat()
    snapshot = {
        'source': 'angel',
        'instrument': inst.as_dict(),
        'price': 25000.0,
        'vix': 14.0,
        'atr': 50.0,
        'atr_ratio': 1.0,
        'trend_strength': 0.8,
        'timestamp': now.isoformat(),
        'vix_timestamp': now.isoformat(),
        'bar_timestamp': bar_start,
        'chart': _sample_candles(now),
        'broker_account': {'available_cash': 500000.0, 'orders': []},
    }

    call_index = 0

    def mock_get_candles(sym, interval='5m'):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            # First symbol throws connection error
            raise ConnectionResetError(10054, "Drop")
        # Subsequent symbols return valid data
        return {'source': 'LIVE_ANGEL_ONE', 'chart': _sample_candles(now)}

    fake_adapter = MagicMock()
    fake_adapter.get_candles.side_effect = mock_get_candles

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=fake_adapter):
        # Must execute without raising exception
        service._scan_and_execute_fo(snapshot, {'cash': 500000.0}, 1)

    last_scan = service.last_scanner_result
    assert last_scan is not None
    assert last_scan['screened_count'] > 1
    # First symbol failed gracefully
    assert 'missing_or_malformed_candles' in last_scan.get('rejected_by_reason', {})
    ledger.close()


# =============================================================================
# 12. SCANNER BATCH ROTATION
# =============================================================================

def test_12_scanner_batch_rotation_works(tmp_path, test_clock):
    """TEST 12: Existing scanner batch rotation still works across consecutive scans."""
    inst = Instrument('NSE', 'Nifty 50', '99926000', 1, 'INDEX')
    vix = Instrument('NSE', 'India VIX', '456', 1, 'INDEX')
    feed = AngelReadOnlyFeed(inst, vix)
    feed.connected = True

    ledger = PaperLedger(tmp_path / "paper_test_rot.sqlite3", initial_cash=100000.0)
    service = PaperTradingService(
        feed,
        ledger,
        calendar=SessionCalendar({'year': 2026}),
        clock=lambda: test_clock[0],
        scanner_batch_size=4,
    )

    now = test_clock[0]
    bar_start = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).isoformat()
    snapshot = {
        'source': 'angel',
        'instrument': inst.as_dict(),
        'price': 25000.0,
        'vix': 14.0,
        'atr': 50.0,
        'atr_ratio': 1.0,
        'trend_strength': 0.8,
        'timestamp': now.isoformat(),
        'vix_timestamp': now.isoformat(),
        'bar_timestamp': bar_start,
        'chart': _sample_candles(now),
        'broker_account': {'available_cash': 500000.0, 'orders': []},
    }

    initial_rot = service._scanner_rotation_idx

    fake_adapter = MagicMock()
    fake_adapter.get_candles.return_value = {
        'source': 'LIVE_ANGEL_ONE',
        'chart': _sample_candles(now),
    }

    with patch('tradingagents.runtime.market_adapter.get_active_market_adapter', return_value=fake_adapter):
        service._scan_and_execute_fo(snapshot, {'cash': 500000.0}, 1)
        first_rot = service._scanner_rotation_idx

        # Advance/reset cadence to simulate next scheduled 5-minute cycle
        service._last_scan_mono = 0.0
        service._scan_and_execute_fo(snapshot, {'cash': 500000.0}, 2)
        second_rot = service._scanner_rotation_idx

    assert first_rot != initial_rot
    assert second_rot != first_rot
    ledger.close()


# =============================================================================
# 13. REFERENCE INDEX VALIDATION GREEN
# =============================================================================

def test_13_reference_index_validation_remains_green(test_clock):
    """TEST 13: Reference INDEX validation regression remains green."""
    now = test_clock[0]
    inst = Instrument('NSE', 'Nifty 50', '99926000', 1, 'INDEX')

    # Reference validation passes
    inst.validate_reference(now)

    # Trade validation strictly raises non_tradable_instrument
    with pytest.raises(ValueError, match="non_tradable_instrument"):
        inst.validate_trade(now)


# =============================================================================
# 14. LIVE EXECUTION REMAINS FALSE
# =============================================================================

def test_14_live_execution_remains_false(instruments, tmp_path, test_clock):
    """TEST 14: live_execution remains false and mode is paper."""
    inst, vix = instruments
    feed = AngelReadOnlyFeed(inst, vix)
    feed.connected = True

    ledger = PaperLedger(tmp_path / "paper_test_live.sqlite3", initial_cash=100000.0)
    service = PaperTradingService(
        feed,
        ledger,
        calendar=SessionCalendar({'year': 2026}),
        clock=lambda: test_clock[0],
    )

    status = service.status()
    assert status.get('live_execution') is False
    assert status.get('mode') == 'paper'
    assert service.fo_orchestrator.paper_engine is not None
    ledger.close()


# =============================================================================
# 15. SCENARIO A: RESPONSE CLOCK PREVENTS FALSE FUTURE REJECTION
# =============================================================================

def test_15_scenario_a_response_clock_prevents_false_future_rejection(instruments):
    """Scenario A: fetch() begins at T0, Angel response arrives at T1 (T0 + 15s),
    exchFeedTime is T0 + 10s (> T0 + 5s future relative to T0, but 5s past relative to T1).
    Assert ACCEPT."""
    inst, vix = instruments
    t0 = datetime(2026, 9, 29, 10, 15, 0, tzinfo=IST)
    clock_state = [t0]

    feed = AngelReadOnlyFeed(inst, vix, clock=lambda: clock_state[0])
    client = MagicMock()
    feed.client = client
    feed.connected = True

    def mock_get_market_data(*args, **kwargs):
        clock_state[0] = t0 + timedelta(seconds=15)  # Response arrives at T1 = T0 + 15s
        quote_time = t0 + timedelta(seconds=10)      # Quote stamped at T0 + 10s
        return {
            'status': True,
            'data': {
                'fetched': [
                    {'symbolToken': inst.token, 'exchange': inst.exchange, 'ltp': 25000.0, 'exchFeedTime': quote_time.isoformat()},
                    {'symbolToken': vix.token, 'exchange': vix.exchange, 'ltp': 14.0, 'exchFeedTime': quote_time.isoformat()},
                ],
                'unfetched': [],
            },
        }

    client.getMarketData.side_effect = mock_get_market_data
    client.getCandleData.return_value = {'status': True, 'data': _sample_candles(t0)}
    client.rmsLimit.return_value = {'status': True, 'data': {'availablecash': '500000.0'}}
    client.position.return_value = {'status': True, 'data': []}
    client.orderBook.return_value = {'status': True, 'data': []}

    snapshot = feed.fetch(t0)
    assert snapshot is not None
    assert snapshot['price'] == 25000.0
    assert snapshot['vix'] == 14.0
    assert snapshot['timestamp'] == (t0 + timedelta(seconds=10)).isoformat()


# =============================================================================
# 16. SCENARIO B: GENUINELY FUTURE QUOTE REJECTED
# =============================================================================

def test_16_scenario_b_genuinely_future_quote_rejected(instruments):
    """Scenario B: Quote genuinely > 5s future relative to response clock.
    Assert REJECT with stale_or_future_market_data."""
    inst, vix = instruments
    t0 = datetime(2026, 9, 29, 10, 15, 0, tzinfo=IST)
    clock_state = [t0]

    feed = AngelReadOnlyFeed(inst, vix, clock=lambda: clock_state[0])
    client = MagicMock()
    feed.client = client
    feed.connected = True

    def mock_get_market_data(*args, **kwargs):
        clock_state[0] = t0 + timedelta(seconds=10)  # T1 = T0 + 10s
        quote_time = clock_state[0] + timedelta(seconds=6)  # 6s future relative to T1 (> 5s tolerance)
        return {
            'status': True,
            'data': {
                'fetched': [
                    {'symbolToken': inst.token, 'exchange': inst.exchange, 'ltp': 25000.0, 'exchFeedTime': quote_time.isoformat()},
                    {'symbolToken': vix.token, 'exchange': vix.exchange, 'ltp': 14.0, 'exchFeedTime': quote_time.isoformat()},
                ],
                'unfetched': [],
            },
        }

    client.getMarketData.side_effect = mock_get_market_data
    client.getCandleData.return_value = {'status': True, 'data': _sample_candles(t0)}

    with pytest.raises(ValueError, match="stale_or_future_market_data"):
        feed.fetch(t0)


# =============================================================================
# 17. SCENARIO C: GENUINELY STALE QUOTE REJECTED
# =============================================================================

def test_17_scenario_c_genuinely_stale_quote_rejected(instruments):
    """Scenario C: Quote genuinely > 90s old relative to response clock.
    Assert REJECT with stale_or_future_market_data."""
    inst, vix = instruments
    t0 = datetime(2026, 9, 29, 10, 15, 0, tzinfo=IST)
    clock_state = [t0]

    feed = AngelReadOnlyFeed(inst, vix, clock=lambda: clock_state[0])
    client = MagicMock()
    feed.client = client
    feed.connected = True

    def mock_get_market_data(*args, **kwargs):
        clock_state[0] = t0 + timedelta(seconds=5)  # T1 = T0 + 5s
        quote_time = clock_state[0] - timedelta(seconds=95)  # 95s stale relative to T1 (> 90s limit)
        return {
            'status': True,
            'data': {
                'fetched': [
                    {'symbolToken': inst.token, 'exchange': inst.exchange, 'ltp': 25000.0, 'exchFeedTime': quote_time.isoformat()},
                    {'symbolToken': vix.token, 'exchange': vix.exchange, 'ltp': 14.0, 'exchFeedTime': quote_time.isoformat()},
                ],
                'unfetched': [],
            },
        }

    client.getMarketData.side_effect = mock_get_market_data
    client.getCandleData.return_value = {'status': True, 'data': _sample_candles(t0)}

    with pytest.raises(ValueError, match="stale_or_future_market_data"):
        feed.fetch(t0)


# =============================================================================
# 18. SCENARIO D: FORMING CANDLE EXCLUDED WITHOUT FUTURE LEAKAGE
# =============================================================================

def test_18_scenario_d_forming_candle_excluded_without_future_leakage(instruments):
    """Scenario D: Latest 1-minute candle is still forming (stamp + 1m > candle_now).
    Assert excluded from completed bars and no future_candle error."""
    inst, vix = instruments
    t0 = datetime(2026, 9, 29, 10, 15, 20, tzinfo=IST)  # 20s into 10:15 bar
    clock_state = [t0]

    feed = AngelReadOnlyFeed(inst, vix, clock=lambda: clock_state[0])
    client = MagicMock()
    feed.client = client
    feed.connected = True

    client.getMarketData.return_value = _valid_quotes(inst, vix, t0)

    # 35 completed bars ending at 10:15:00
    base_candles = _sample_candles(t0.replace(second=0), count=35)
    # Plus a currently forming bar that started at 10:15:00 (ends at 10:16:00 > 10:15:20)
    forming_bar = [t0.replace(second=0).isoformat(), 105.0, 105.5, 104.5, 105.2, 1000]
    all_candles = base_candles + [forming_bar]

    client.getCandleData.return_value = {'status': True, 'data': all_candles}
    client.rmsLimit.return_value = {'status': True, 'data': {'availablecash': '500000.0'}}
    client.position.return_value = {'status': True, 'data': []}
    client.orderBook.return_value = {'status': True, 'data': []}

    snapshot = feed.fetch(t0)
    assert snapshot is not None
    # Forming bar (10:15:00) must be excluded; last completed bar timestamp is 10:14:00
    expected_last_completed = (t0.replace(second=0) - timedelta(minutes=1)).isoformat()
    assert snapshot['bar_timestamp'] == expected_last_completed


# =============================================================================
# 19. SCENARIO E: COMPLETED CANDLE FRESH WITHIN 180S ACCEPTED
# =============================================================================

def test_19_scenario_e_completed_candle_fresh_within_180s_accepted(instruments):
    """Scenario E: Last completed candle <= 180s old relative to response clock.
    Assert ACCEPT."""
    inst, vix = instruments
    t0 = datetime(2026, 9, 29, 10, 17, 0, tzinfo=IST)
    clock_state = [t0]

    feed = AngelReadOnlyFeed(inst, vix, clock=lambda: clock_state[0])
    client = MagicMock()
    feed.client = client
    feed.connected = True

    client.getMarketData.return_value = _valid_quotes(inst, vix, t0)

    # Last completed bar closed at 10:15:00 (age is 120s relative to 10:17:00, <= 180s)
    candles = _sample_candles(datetime(2026, 9, 29, 10, 15, 0, tzinfo=IST), count=35)
    client.getCandleData.return_value = {'status': True, 'data': candles}
    client.rmsLimit.return_value = {'status': True, 'data': {'availablecash': '500000.0'}}
    client.position.return_value = {'status': True, 'data': []}
    client.orderBook.return_value = {'status': True, 'data': []}

    snapshot = feed.fetch(t0)
    assert snapshot is not None
    assert snapshot['bar_timestamp'] == datetime(2026, 9, 29, 10, 14, 0, tzinfo=IST).isoformat()


# =============================================================================
# 20. SCENARIO F: COMPLETED CANDLE STALE OVER 180S REJECTED
# =============================================================================

def test_20_scenario_f_completed_candle_stale_over_180s_rejected(instruments):
    """Scenario F: Completed candle genuinely stale > 180s.
    Assert REJECT with stale_or_future_market_data."""
    inst, vix = instruments
    t0 = datetime(2026, 9, 29, 10, 20, 0, tzinfo=IST)
    clock_state = [t0]

    feed = AngelReadOnlyFeed(inst, vix, clock=lambda: clock_state[0])
    client = MagicMock()
    feed.client = client
    feed.connected = True

    client.getMarketData.return_value = _valid_quotes(inst, vix, t0)

    # Last completed bar closed at 10:16:00 (age is 240s relative to 10:20:00, > 180s)
    candles = _sample_candles(datetime(2026, 9, 29, 10, 16, 0, tzinfo=IST), count=35)
    client.getCandleData.return_value = {'status': True, 'data': candles}

    with pytest.raises(ValueError, match="stale_or_future_market_data"):
        feed.fetch(t0)


# =============================================================================
# 21. CANDLE RATE LIMIT DIAGNOSTIC MARKERS AND PAYLOAD SAFETY
# =============================================================================

@pytest.mark.parametrize(
    "exc_class,exc_msg,expected_marker",
    [
        (Exception, "Error: AB1004 Rate limit reached. secret_token=JWT_TOP_SECRET payload={'symboltoken': '99926000'}", "AB1004"),
        (RuntimeError, "HTTP 429 Too Many Requests. Headers: {'Authorization': 'Bearer SECRET_TOKEN_123'}", "429"),
        (ValueError, "Access denied because of exceeding access rate. user=USR_SECRET pin=9999", "exceeding access rate"),
        (Exception, "Client error: too many requests. symbol=NIFTY_TOKEN_12345", "too many requests"),
    ],
)
def test_21_candle_rate_limit_diagnostic_markers_and_payload_safety(instruments, caplog, exc_class, exc_msg, expected_marker):
    """Verify getCandleData rate limit broker exceptions safely log only the recognized marker and exception type,
    with zero leakage of payload, symbols, or credentials."""
    from tradingagents.runtime.rate_limiter import get_angel_rate_limiter

    limiter = get_angel_rate_limiter()
    limiter.reset()

    inst, vix = instruments
    t0 = datetime(2026, 9, 29, 10, 15, 0, tzinfo=IST)
    feed = AngelReadOnlyFeed(inst, vix, clock=lambda: t0, limiter=limiter)
    client = MagicMock()
    feed.client = client
    feed.connected = True

    client.getMarketData.return_value = _valid_quotes(inst, vix, t0)
    client.getCandleData.side_effect = exc_class(exc_msg)

    with caplog.at_level(logging.WARNING):
        caplog.clear()
        with pytest.raises(ValueError, match="angel_rate_limited"):
            feed.fetch(t0)

    # 1. Assert structured line appears
    expected_detail = f"Angel rate-limit detail: action=getCandleData bucket=candle attempt=1 exception_type={exc_class.__name__} marker={expected_marker}"
    assert any(expected_detail in record.message for record in caplog.records), (
        f"Expected '{expected_detail}' in captured logs: {[r.message for r in caplog.records]}"
    )

    # 2. Assert safe rate limit action/bucket line also appears
    expected_action = "Angel rate limit: action=getCandleData bucket=candle source=broker_exception attempt=1"
    assert any(expected_action in record.message for record in caplog.records)

    # 3. Assert STRICT security: secrets, credentials, symbol tokens, payloads NEVER appear in any record
    for record in caplog.records:
        assert "JWT_TOP_SECRET" not in record.message
        assert "SECRET_TOKEN_123" not in record.message
        assert "USR_SECRET" not in record.message
        assert "9999" not in record.message
        assert "NIFTY_TOKEN_12345" not in record.message
        assert "symboltoken" not in record.message
        assert "payload" not in record.message
        assert "Headers" not in record.message
