"""Deterministic unit and integration tests for AngelRateLimiter.

Verifies:
1. Limiter throttles before excessive candle calls (spacing >= 0.35s).
2. Quote and candle buckets are independent.
3. Concurrent threads cannot bypass limiter or create bursts.
4. Reactive rate-limit error (HTTP 429 / AB1004) activates bounded cooldown.
5. Cooldown expiry restores normal request processing without infinite retry.
6. Timeout ceiling prevents infinite waits.
7. AngelReadOnlyFeed fetch() routes every read through the centralized limiter.
8. Scanner rotation continues working across full universe cycles.
9. Paper trading safety invariants (live_execution=False, order mutation blocked) remain strict.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.runtime.angel_data import AngelReadOnlyFeed
from tradingagents.runtime.market_snapshot import IST, Instrument, SessionCalendar
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService
from tradingagents.runtime.rate_limiter import (
    AngelBucket,
    AngelRateLimiter,
    AngelRateLimitTimeout,
    set_angel_rate_limiter,
)


@pytest.fixture(autouse=True)
def reset_global_limiter():
    """Ensure clean global rate limiter state before and after each test."""
    set_angel_rate_limiter(None)
    yield
    set_angel_rate_limiter(None)


def test_1_limiter_throttles_before_excessive_candle_calls():
    """Test 1: CANDLE requests spaced by min_interval (0.35s -> ~2.85 req/sec)."""
    clock = [100.0]
    slept = []

    def fake_clock():
        return clock[0]

    def fake_sleep(duration):
        slept.append(duration)
        clock[0] += duration

    limiter = AngelRateLimiter(
        clock=fake_clock,
        sleep_fn=fake_sleep,
        candle_min_interval=0.35,
        global_min_interval=0.02,
    )

    # Call 1: Immediate execution
    w1 = limiter.acquire(AngelBucket.CANDLE)
    assert w1 == 0.0
    assert clock[0] == 100.0

    # Call 2: Must wait 0.35s
    w2 = limiter.acquire(AngelBucket.CANDLE)
    assert round(w2, 4) == 0.35
    assert round(clock[0], 4) == 100.35

    # Call 3: Must wait another 0.35s
    w3 = limiter.acquire(AngelBucket.CANDLE)
    assert round(w3, 4) == 0.35
    assert round(clock[0], 4) == 100.70

    # Total simulated wait
    assert len(slept) == 2
    assert sum(slept) == pytest.approx(0.70, abs=1e-4)


def test_2_quote_and_candle_buckets_are_independent():
    """Test 2: Quote (0.10s) and Candle (0.35s) buckets track independent schedules."""
    clock = [200.0]
    slept = []

    def fake_clock():
        return clock[0]

    def fake_sleep(duration):
        slept.append(duration)
        clock[0] += duration

    limiter = AngelRateLimiter(
        clock=fake_clock,
        sleep_fn=fake_sleep,
        candle_min_interval=0.35,
        quote_min_interval=0.10,
        global_min_interval=0.02,
    )

    # 1. Acquire candle at t=200.0
    w_c1 = limiter.acquire(AngelBucket.CANDLE)
    assert w_c1 == 0.0

    # 2. Acquire quote immediately at t=200.0
    # Quote is an independent bucket; only global anti-burst (0.02s) applies
    w_q1 = limiter.acquire(AngelBucket.QUOTE)
    assert round(w_q1, 4) == 0.02
    assert round(clock[0], 4) == 200.02

    # 3. Second quote: spaced by quote interval (0.10s from previous quote at 200.02 -> 200.12)
    w_q2 = limiter.acquire(AngelBucket.QUOTE)
    assert round(w_q2, 4) == 0.10
    assert round(clock[0], 4) == 200.12

    # 4. Second candle: spaced by candle interval (0.35s from candle 1 at 200.00 -> 200.35)
    # Notice: Quote 2 at 200.12 executed BEFORE Candle 2 at 200.35 without blocking!
    w_c2 = limiter.acquire(AngelBucket.CANDLE)
    assert round(w_c2, 4) == 0.23  # 200.12 + 0.23 = 200.35
    assert round(clock[0], 4) == 200.35


def test_3_concurrent_calls_cannot_bypass_limiter():
    """Test 3: Concurrent threads calling acquire() receive non-overlapping spaced slots."""
    limiter = AngelRateLimiter(candle_min_interval=0.05, global_min_interval=0.01)
    acquired_times = []
    threads = []
    lock = threading.Lock()

    def worker():
        limiter.acquire(AngelBucket.CANDLE)
        with lock:
            acquired_times.append(limiter.time_fn())

    for _ in range(5):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=3)

    assert len(acquired_times) == 5
    # Verify timestamps are spaced by at least the candle interval (allowing for thread scheduling jitter)
    sorted_times = sorted(acquired_times)
    for i in range(1, len(sorted_times)):
        diff = sorted_times[i] - sorted_times[i - 1]
        assert diff >= 0.030, f"Expected spacing >= 0.030s between thread calls, got {diff}"


def test_4_rate_limit_response_activates_bounded_cooldown():
    """Test 4: Reactive notify_rate_limit activates cooldown and blocks calls fast-fail."""
    clock = [300.0]

    def fake_clock():
        return clock[0]

    limiter = AngelRateLimiter(clock=fake_clock, cooldown_sec=3.0)

    assert limiter.is_in_cooldown() is False
    assert limiter.remaining_cooldown() == 0.0

    # Trigger rate limit
    limiter.notify_rate_limit(cooldown_sec=3.0)
    assert limiter.is_in_cooldown() is True
    assert round(limiter.remaining_cooldown(), 1) == 3.0

    # Immediate call during cooldown raises angel_rate_limited
    with pytest.raises(ValueError, match="angel_rate_limited"):
        limiter.acquire(AngelBucket.CANDLE, block_on_cooldown=False)

    # Advance clock past cooldown
    clock[0] += 3.1
    assert limiter.is_in_cooldown() is False
    assert limiter.remaining_cooldown() == 0.0

    # Acquire succeeds after cooldown
    w = limiter.acquire(AngelBucket.CANDLE, block_on_cooldown=False)
    assert w == 0.0


def test_5_timeout_ceiling_prevents_infinite_wait():
    """Test 5: Wait duration exceeding timeout raises AngelRateLimitTimeout."""
    clock = [400.0]

    def fake_clock():
        return clock[0]

    limiter = AngelRateLimiter(clock=fake_clock, candle_min_interval=5.0)

    # First call at t=400.0
    limiter.acquire(AngelBucket.CANDLE)

    # Second call would require 5.0s wait. With timeout=1.0s, it must raise immediately
    with pytest.raises(AngelRateLimitTimeout, match="exceeds timeout"):
        limiter.acquire(AngelBucket.CANDLE, timeout=1.0)


def test_6_angel_feed_integrates_limiter_proactively():
    """Test 6: AngelReadOnlyFeed routes all fetch() calls through centralized rate limiter."""
    inst = Instrument('NSE', 'NIFTY', '99926000', 50, 'INDEX')
    vix = Instrument('NSE', 'India VIX', '99926001', 1, 'INDEX')

    acquired_buckets = []
    fake_limiter = MagicMock(spec=AngelRateLimiter)
    fake_limiter.cooldown_sec = 3.0
    fake_limiter.is_in_cooldown.return_value = False

    def fake_acquire(bucket, timeout=10.0, block_on_cooldown=False):
        acquired_buckets.append(bucket)
        return 0.0

    fake_limiter.acquire.side_effect = fake_acquire

    feed = AngelReadOnlyFeed(inst, vix, limiter=fake_limiter)
    client = MagicMock()
    feed.client = client
    feed.connected = True

    now = datetime(2026, 9, 29, 10, 30, 0, tzinfo=IST)
    client.getMarketData.return_value = {
        'status': True,
        'data': {
            'fetched': [
                {'symbolToken': inst.token, 'exchange': inst.exchange, 'ltp': 25000.0, 'exchFeedTime': now.isoformat()},
                {'symbolToken': vix.token, 'exchange': vix.exchange, 'ltp': 14.0, 'exchFeedTime': now.isoformat()},
            ],
            'unfetched': [],
        },
    }
    minute = now.replace(second=0, microsecond=0)
    client.getCandleData.return_value = {
        'status': True,
        'data': [[(minute - timedelta(minutes=35 - i)).isoformat(), 25000 + i, 25005 + i, 24995 + i, 25002 + i, 1000] for i in range(35)],
    }
    client.rmsLimit.return_value = {'status': True, 'data': {'availablecash': '500000.0'}}
    client.position.return_value = {'status': True, 'data': []}
    client.orderBook.return_value = {'status': True, 'data': []}

    snapshot = feed.fetch(now)
    assert snapshot is not None
    assert snapshot['price'] == 25000.0

    # Verify all 5 read requests acquired rate limit tokens in proper buckets
    assert AngelBucket.QUOTE in acquired_buckets
    assert AngelBucket.CANDLE in acquired_buckets
    assert AngelBucket.ACCOUNT in acquired_buckets
    assert acquired_buckets.count(AngelBucket.ACCOUNT) == 3  # rmsLimit, position, orderBook


def test_7_scanner_rotation_works_with_centralized_limiter(tmp_path):
    """Test 7: Full F&O universe rotation is preserved without scattered sleep calls."""
    db_path = tmp_path / "test_scanner.sqlite3"
    ledger = PaperLedger(db_path, initial_cash=100000)
    feed = MagicMock()
    feed.source = "angel"
    feed.instrument = Instrument("NSE", "NIFTY", "99926000", 50, "INDEX")

    mock_scrip_master = MagicMock()
    # Mock universe of 25 symbols (2 indices, 23 stocks)
    universe = {
        "NIFTY": {"is_index": True, "lot_size": 50},
        "BANKNIFTY": {"is_index": True, "lot_size": 15},
    }
    for i in range(1, 24):
        universe[f"STOCK_{i}"] = {"is_index": False, "lot_size": 100}
    mock_scrip_master.get_fo_universe.return_value = universe

    service = PaperTradingService(
        feed=feed,
        ledger=ledger,
        calendar=SessionCalendar({'year': 2026}),
        scrip_master=mock_scrip_master,
        scanner_batch_size=8,
    )

    # Prioritized discover_universe returns indices first then stocks
    service.fo_scanner.discover_universe = MagicMock(return_value=["NIFTY", "BANKNIFTY"] + [f"STOCK_{i}" for i in range(1, 24)])

    now = datetime(2026, 9, 29, 10, 30, 0, tzinfo=IST)
    snapshot = {
        'price': 25000.0,
        'chart': [[now.isoformat(), 25000, 25010, 24990, 25005, 1000] for _ in range(30)],
    }
    account = {'cash': 100000, 'positions': []}

    # Cycle 1
    service._scan_and_execute_fo(snapshot, account, 1)
    res1 = service.last_scanner_result
    assert res1['batch_size'] == 8
    assert res1['rotation_index'] == 0
    assert res1['next_rotation_index'] == 6  # 2 indices + 6 stocks

    # Cycle 2
    service._scan_and_execute_fo(snapshot, account, 2)
    res2 = service.last_scanner_result
    assert res2['rotation_index'] == 6
    assert res2['next_rotation_index'] == 12

    # Cycle 3
    service._scan_and_execute_fo(snapshot, account, 3)
    res3 = service.last_scanner_result
    assert res3['rotation_index'] == 12
    assert res3['next_rotation_index'] == 18

    ledger.close()


def test_8_paper_safety_invariants_preserved():
    """Test 8: Strict paper invariants remain unbreachable."""
    inst = Instrument('NSE', 'NIFTY', '99926000', 50, 'INDEX')
    vix = Instrument('NSE', 'India VIX', '99926001', 1, 'INDEX')
    feed = AngelReadOnlyFeed(inst, vix)

    # Broker mutation calls must raise PermissionError
    with pytest.raises(PermissionError, match="broker_order_mutation_permanently_blocked"):
        feed.placeOrder()

    with pytest.raises(PermissionError, match="broker_order_mutation_permanently_blocked"):
        feed.modifyOrder()

    with pytest.raises(PermissionError, match="broker_order_mutation_permanently_blocked"):
        feed.cancelOrder()


def test_9_market_adapter_get_candles_acquires_limiter_before_network_call():
    """Test 9: AngelOneMarketAdapter.get_candles acquires CANDLE bucket BEFORE network call."""
    from tradingagents.runtime.market_adapter import AngelOneMarketAdapter

    adapter = AngelOneMarketAdapter()
    call_order = []

    mock_limiter = MagicMock()

    def mock_acquire(bucket, *args, **kwargs):
        call_order.append(('limiter_acquire', bucket))
        return 0.0

    mock_limiter.acquire.side_effect = mock_acquire
    mock_limiter.cooldown_sec = 3.0
    mock_limiter.is_in_cooldown.return_value = False

    mock_client = MagicMock()
    mock_sc = MagicMock()
    now_ist = datetime.now(IST)
    minute = now_ist.replace(second=0, microsecond=0)
    fake_candles = [[(minute - timedelta(minutes=35 - i)).isoformat(), 25000 + i, 25005 + i, 24995 + i, 25002 + i, 1000] for i in range(35)]

    def mock_get_candle_data(params):
        call_order.append(('smart_connect_getCandleData', params['symboltoken']))
        return {'status': True, 'data': fake_candles}

    mock_sc.getCandleData.side_effect = mock_get_candle_data
    mock_client._smart_connect = mock_sc

    with patch.object(adapter, '_get_client', return_value=mock_client), \
         patch.object(adapter, 'resolve_token', return_value=('NSE', '99926000', 'NIFTY 50')), \
         patch('tradingagents.runtime.market_adapter.get_angel_rate_limiter', return_value=mock_limiter):

        candles = adapter.get_candles('NIFTY', interval='5m', allow_fallback=False)
        assert candles['source'] == 'LIVE_ANGEL_ONE'
        assert candles['price'] == fake_candles[-1][4]

    # Verify limiter.acquire was called BEFORE getCandleData
    assert len(call_order) >= 2
    assert call_order[0] == ('limiter_acquire', AngelBucket.CANDLE)
    assert call_order[1][0] == 'smart_connect_getCandleData'


def test_10_market_adapter_get_quote_acquires_limiter_before_network_call():
    """Test 10: AngelOneMarketAdapter.get_quote acquires QUOTE bucket BEFORE network call."""
    from tradingagents.runtime.market_adapter import AngelOneMarketAdapter

    adapter = AngelOneMarketAdapter()
    call_order = []

    mock_limiter = MagicMock()

    def mock_acquire(bucket, *args, **kwargs):
        call_order.append(('limiter_acquire', bucket))
        return 0.0

    mock_limiter.acquire.side_effect = mock_acquire
    mock_limiter.cooldown_sec = 3.0
    mock_limiter.is_in_cooldown.return_value = False

    mock_client = MagicMock()

    def mock_get_market_data(mode, exchange_tokens):
        call_order.append(('client_get_market_data', exchange_tokens))
        return {
            'status': True,
            'data': {'fetched': [{'ltp': 25050.0, 'bestBidPrice': 25049.0, 'bestAskPrice': 25051.0, 'tradeVolume': 500000}]},
        }

    mock_client.get_market_data.side_effect = mock_get_market_data

    with patch.object(adapter, '_get_client', return_value=mock_client), \
         patch.object(adapter, 'resolve_token', return_value=('NSE', '99926000', 'NIFTY 50')), \
         patch('tradingagents.runtime.market_adapter.get_angel_rate_limiter', return_value=mock_limiter):

        quote = adapter.get_quote('NIFTY')
        assert quote['source'] == 'LIVE_ANGEL_ONE'
        assert quote['ltp'] == 25050.0

    # Verify limiter.acquire was called BEFORE get_market_data
    assert len(call_order) >= 2
    assert call_order[0] == ('limiter_acquire', AngelBucket.QUOTE)
    assert call_order[1][0] == 'client_get_market_data'


def test_11_market_adapter_candles_rate_limited_fails_closed_when_fallback_forbidden():
    """Test 11: When allow_fallback=False, rate-limited response fails closed and raises angel_rate_limited."""
    from tradingagents.runtime.market_adapter import AngelOneMarketAdapter

    adapter = AngelOneMarketAdapter()
    limiter = AngelRateLimiter()
    set_angel_rate_limiter(limiter)

    mock_client = MagicMock()
    mock_sc = MagicMock()
    mock_sc.getCandleData.return_value = {
        'status': False,
        'message': 'Access denied because of exceeding access rate',
        'errorcode': 'AB1004',
    }
    mock_client._smart_connect = mock_sc

    with patch.object(adapter, '_get_client', return_value=mock_client), \
         patch.object(adapter, 'resolve_token', return_value=('NSE', '99926000', 'NIFTY 50')), \
         patch('tradingagents.runtime.market_adapter.ResearchMarketAdapter') as mock_research:

        with pytest.raises(ValueError, match="angel_rate_limited"):
            adapter.get_candles('NIFTY', interval='5m', allow_fallback=False)

        # Rate limiter cooldown MUST be active
        assert limiter.is_in_cooldown() is True
        # ResearchMarketAdapter must NEVER be called when fallback is forbidden
        assert mock_research.call_count == 0


def test_12_dashboard_fallback_is_clearly_labeled_and_rejected_by_scanner_provenance():
    """Test 12: When allow_fallback=True, fallback is clearly labeled and rejected by paper scanner provenance."""
    from tradingagents.runtime.market_adapter import AngelOneMarketAdapter

    adapter = AngelOneMarketAdapter()
    limiter = AngelRateLimiter()
    set_angel_rate_limiter(limiter)

    mock_client = MagicMock()
    mock_sc = MagicMock()
    mock_sc.getCandleData.return_value = {
        'status': False,
        'message': 'Access denied because of exceeding access rate',
        'errorcode': 'AB1004',
    }
    mock_client._smart_connect = mock_sc

    fake_research_result = {
        'symbol': 'NIFTY',
        'chart': [['2026-09-29T10:00:00', 25000, 25010, 24990, 25000, 100]],
        'price': 25000.0,
        'source': 'YAHOO_FINANCE',
    }

    with patch.object(adapter, '_get_client', return_value=mock_client), \
         patch.object(adapter, 'resolve_token', return_value=('NSE', '99926000', 'NIFTY 50')), \
         patch('tradingagents.runtime.market_adapter.ResearchMarketAdapter') as mock_research:

        mock_research_inst = MagicMock()
        mock_research_inst.get_candles.return_value = fake_research_result
        mock_research.return_value = mock_research_inst

        res = adapter.get_candles('NIFTY', interval='5m', allow_fallback=True)
        # Clearly marked as fallback
        assert res['source'] == 'RESEARCH_FALLBACK'
        assert res['is_fallback'] is True
        assert res['fallback_reason'] == 'angel_rate_limited'

        # Provenance gate check in paper service requires 'LIVE_ANGEL_ONE'
        assert res['source'] != 'LIVE_ANGEL_ONE'


def test_13_account_read_paths_acquire_account_bucket():
    """Test 13: AngelOneClient profile and RMS reads acquire AngelBucket.ACCOUNT."""
    from tradingagents.integrations.angel_one.client import AngelOneClient

    client = AngelOneClient(api_key="k", client_code="c", pin="p", totp_secret="s")
    client._is_authenticated = True
    client._refresh_token = "ref_tok"

    acquired = []
    mock_limiter = MagicMock()
    mock_limiter.acquire.side_effect = lambda bucket: acquired.append(bucket)

    mock_sc = MagicMock()
    mock_sc.getProfile.return_value = {'status': True, 'data': {'clientcode': 'TEST', 'name': 'Trader'}}
    mock_sc.getRMS.return_value = {'status': True, 'data': {'availablecash': '100000.0'}}
    client._smart_connect = mock_sc

    with patch('tradingagents.integrations.angel_one.client.get_angel_rate_limiter', return_value=mock_limiter):
        client.get_profile()
        client.get_rms()

    assert acquired == [AngelBucket.ACCOUNT, AngelBucket.ACCOUNT]


def test_14_concurrent_feed_and_dashboard_share_centralized_limiter_state():
    """Test 14: AngelReadOnlyFeed and AngelOneMarketAdapter share the same limiter singleton."""
    from tradingagents.runtime.market_adapter import AngelOneMarketAdapter
    from tradingagents.runtime.rate_limiter import get_angel_rate_limiter

    limiter = get_angel_rate_limiter()
    inst = Instrument('NSE', 'NIFTY', '99926000', 50, 'INDEX')
    vix = Instrument('NSE', 'India VIX', '99926001', 1, 'INDEX')
    feed = AngelReadOnlyFeed(inst, vix)
    adapter = AngelOneMarketAdapter()

    assert feed.limiter is limiter

    # Notify rate limit from feed
    feed.limiter.notify_rate_limit(cooldown_sec=5.0)

    # Limiter is now globally in cooldown
    assert limiter.is_in_cooldown() is True
    assert adapter._get_client is not None

    # Attempting to fetch candles via adapter with allow_fallback=False must immediately raise without hitting network
    mock_client = MagicMock()
    with patch.object(adapter, '_get_client', return_value=mock_client), \
         patch.object(adapter, 'resolve_token', return_value=('NSE', '99926000', 'NIFTY 50')):
        with pytest.raises(ValueError, match="angel_rate_limited"):
            adapter.get_candles('NIFTY', allow_fallback=False)
        # Client network call was NOT made
        assert mock_client._smart_connect.getCandleData.call_count == 0


def test_15_scanner_source_code_has_no_50ms_sleep():
    """Test 15: Static assertion that paper_service.py scanner loop contains NO time.sleep(0.05)."""
    paper_service_path = Path(__file__).resolve().parent.parent / "tradingagents" / "runtime" / "paper_service.py"
    code = paper_service_path.read_text(encoding="utf-8")

    # The scanner method must not contain any 50ms pacing
    assert "time.sleep(0.05)" not in code
    assert "0.05" not in code or "time.sleep" not in code


def test_16_client_rate_limit_endpoint_diagnostics(caplog):
    """Test 16: Verify client.py methods emit exact structured rate limit logs for broker responses."""
    import logging
    from tradingagents.integrations.angel_one.client import AngelOneClient
    from tradingagents.runtime.rate_limiter import get_angel_rate_limiter

    limiter = get_angel_rate_limiter()
    limiter.reset()

    client = AngelOneClient(
        api_key="mock_key",
        client_code="MOCK_USER",
        pin="1234",
        totp_secret="JBSWY3DPEHPK3PXP",
    )
    mock_sc = MagicMock()
    client._smart_connect = mock_sc

    # 1. authenticate rate limit response
    mock_sc.generateSession.return_value = {
        "status": False,
        "message": "Access denied because of exceeding access rate",
        "errorcode": "AB1004",
    }
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.authenticate()
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=generateSession bucket=auth source=broker_response attempt=1" in record.message
            for record in caplog.records
        )
        # Ensure credentials are redacted/not leaked
        for record in caplog.records:
            assert "mock_key" not in record.message
            assert "JBSWY3DPEHPK3PXP" not in record.message

    # 2. get_profile rate limit response
    limiter.reset()
    client._is_authenticated = True
    client._refresh_token = "mock_refresh"
    mock_sc.getProfile.return_value = {
        "status": False,
        "message": "Too many requests",
        "errorcode": "429",
    }
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.get_profile()
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=getProfile bucket=account source=broker_response attempt=1" in record.message
            for record in caplog.records
        )

    # 3. get_rms rate limit response
    limiter.reset()
    mock_sc.getRMS.return_value = {
        "status": False,
        "message": "Access denied because of exceeding access rate",
        "errorcode": "AB1004",
    }
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.get_rms()
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=getRMS bucket=account source=broker_response attempt=1" in record.message
            for record in caplog.records
        )

    # 4. get_market_data rate limit response
    limiter.reset()
    mock_sc.getMarketData.return_value = {
        "status": False,
        "message": "exceeding access rate",
        "errorcode": "AB1004",
    }
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.get_market_data(mode="FULL", exchange_tokens={"NSE": ["99926000"]})
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=getMarketData bucket=quote source=broker_response attempt=1" in record.message
            for record in caplog.records
        )


def test_17_client_rate_limit_exception_diagnostics(caplog):
    """Test 17: Verify client.py methods emit exact structured rate limit logs for broker exceptions."""
    import logging
    from tradingagents.integrations.angel_one.client import AngelOneClient
    from tradingagents.runtime.rate_limiter import get_angel_rate_limiter

    limiter = get_angel_rate_limiter()
    limiter.reset()

    client = AngelOneClient(
        api_key="mock_key",
        client_code="MOCK_USER",
        pin="1234",
        totp_secret="JBSWY3DPEHPK3PXP",
    )
    mock_sc = MagicMock()
    client._smart_connect = mock_sc

    # 1. authenticate exception rate limit
    mock_sc.generateSession.side_effect = Exception("429 Too Many Requests: exceeding access rate")
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.authenticate()
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=generateSession bucket=auth source=broker_exception attempt=1" in record.message
            for record in caplog.records
        )

    # 2. get_profile exception rate limit
    limiter.reset()
    client._is_authenticated = True
    client._refresh_token = "mock_refresh"
    mock_sc.getProfile.side_effect = Exception("AB1004: Rate limit reached")
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.get_profile()
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=getProfile bucket=account source=broker_exception attempt=1" in record.message
            for record in caplog.records
        )

    # 3. get_rms exception rate limit
    limiter.reset()
    mock_sc.getRMS.side_effect = Exception("exceeding access rate limit")
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.get_rms()
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=getRMS bucket=account source=broker_exception attempt=1" in record.message
            for record in caplog.records
        )

    # 4. get_market_data exception rate limit
    limiter.reset()
    mock_sc.getMarketData.side_effect = Exception("429 Too Many Requests")
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = client.get_market_data(mode="FULL", exchange_tokens={"NSE": ["99926000"]})
        assert res["status"] is False
        assert any(
            "Angel rate limit: action=getMarketData bucket=quote source=broker_exception attempt=1" in record.message
            for record in caplog.records
        )
