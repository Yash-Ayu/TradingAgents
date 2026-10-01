"""Read-only Angel feed. This module never invokes broker order mutations."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

from .market_snapshot import IST, Instrument, fresh, number, technical_snapshot, timestamp
from .rate_limiter import AngelBucket, get_angel_rate_limiter
from .security import (
    extract_rate_limit_marker,
    install_secret_redaction,
    is_auth_error,
    is_rate_limit,
    is_transient_network_error,
    register_secret,
)

logger = logging.getLogger(__name__)

# Ensure redaction filter is attached to loggers immediately
install_secret_redaction()

ACTION_BUCKET_MAP = {
    'getMarketData': AngelBucket.QUOTE,
    'get_market_data': AngelBucket.QUOTE,
    'getCandleData': AngelBucket.CANDLE,
    'rmsLimit': AngelBucket.ACCOUNT,
    'position': AngelBucket.ACCOUNT,
    'orderBook': AngelBucket.ACCOUNT,
    'getProfile': AngelBucket.ACCOUNT,
    'getRMS': AngelBucket.ACCOUNT,
    'generateSession': AngelBucket.AUTH,
}


class AngelReadOnlyFeed:
    source = 'angel'

    def __init__(self, instrument: Instrument, vix_instrument: Instrument, *, client_factory=None, clock=None, limiter=None):
        self.instrument = instrument
        self.vix_instrument = vix_instrument
        self.client_factory = client_factory
        self._custom_clock = clock is not None
        self.clock = clock or (lambda: datetime.now(IST))
        self.limiter = limiter or get_angel_rate_limiter()
        self.client = None
        self.connected = False
        self._last_auth_time: float = 0.0
        self._last_rate_limit_time: float = 0.0
        self._rate_limit_cooldown_sec: float = self.limiter.cooldown_sec
        self._cached_broker_account: tuple[float, dict[str, Any]] | None = None
        self._broker_account_ttl_sec: float = float(os.environ.get("ANGEL_ACCOUNT_CACHE_TTL", 30.0))

    # Hard-block any broker mutation APIs on this read-only feed
    def placeOrder(self, *args, **kwargs):
        raise PermissionError("broker_order_mutation_permanently_blocked")

    def modifyOrder(self, *args, **kwargs):
        raise PermissionError("broker_order_mutation_permanently_blocked")

    def cancelOrder(self, *args, **kwargs):
        raise PermissionError("broker_order_mutation_permanently_blocked")

    def connect(self, force: bool = False):
        if self.client is not None and not force:
            self.connected = True
            return

        now_mono = time.monotonic()
        if force and (now_mono - self._last_auth_time < 5.0):
            # Throttle authentication attempts: max 1 per 5 seconds
            raise ValueError('angel_auth_failed')

        names = ('ANGEL_API_KEY', 'ANGEL_CLIENT_ID', 'ANGEL_MPIN', 'ANGEL_TOTP_SECRET')
        values = [os.environ.get(name) for name in names]
        if not all(values):
            raise ValueError('angel_credentials_missing')
        for v in values:
            register_secret(v)

        import pyotp

        if self.client_factory is None:
            from SmartApi import SmartConnect
            factory = SmartConnect
        else:
            factory = self.client_factory

        try:
            client = factory(api_key=values[0], timeout=10)
            self.limiter.acquire(AngelBucket.AUTH)
            response = client.generateSession(values[1], values[2], pyotp.TOTP(values[3]).now())
            if is_rate_limit(response):
                self._last_rate_limit_time = time.monotonic()
                logger.warning(
                    "Angel rate limit: action=generateSession bucket=auth source=broker_response attempt=1"
                )
                self.limiter.notify_rate_limit(self._rate_limit_cooldown_sec)
                raise ValueError('angel_rate_limited') from None
            data = self._data(response)
            if isinstance(data, dict):
                register_secret(data.get('jwtToken'))
                register_secret(data.get('refreshToken'))
                register_secret(data.get('feedToken'))
            self.client = client
            self.connected = True
            self._last_auth_time = time.monotonic()
        except Exception as exc:
            self.connected = False
            self.client = None
            if str(exc) == 'angel_rate_limited':
                raise
            if is_rate_limit(exc):
                self._last_rate_limit_time = time.monotonic()
                logger.warning(
                    "Angel rate limit: action=generateSession bucket=auth source=broker_exception attempt=1"
                )
                marker = extract_rate_limit_marker(exc)
                exc_type = type(exc).__name__
                logger.warning(
                    f"Angel rate-limit detail: action=generateSession bucket=auth attempt=1 exception_type={exc_type} marker={marker}"
                )
                self.limiter.notify_rate_limit(self._rate_limit_cooldown_sec)
                raise ValueError('angel_rate_limited') from None
            if is_transient_network_error(exc):
                raise ValueError('angel_connection_error') from None
            raise ValueError('angel_auth_failed') from None

    def _call_with_retry(self, fn, action_name: str, max_retries: int = 2):
        """Execute an idempotent read-only call with bounded retry, backoff, and auth recovery."""
        bucket = ACTION_BUCKET_MAP.get(action_name, AngelBucket.CANDLE)
        b_name = bucket.value if hasattr(bucket, 'value') else str(bucket)

        if self.limiter.is_in_cooldown() or (time.monotonic() - self._last_rate_limit_time < self._rate_limit_cooldown_sec):
            logger.warning(
                f"Angel rate limit: action={action_name} bucket={b_name} source=proactive_cooldown attempt=0"
            )
            raise ValueError('angel_rate_limited')

        reauthed = False
        for attempt in range(max_retries + 1):
            attempt_num = attempt + 1
            try:
                self.limiter.acquire(bucket)
                res = fn()
                if isinstance(res, dict):
                    if is_rate_limit(res):
                        self._last_rate_limit_time = time.monotonic()
                        logger.warning(
                            f"Angel rate limit: action={action_name} bucket={b_name} source=broker_response attempt={attempt_num}"
                        )
                        self.limiter.notify_rate_limit(self._rate_limit_cooldown_sec)
                        raise ValueError('angel_rate_limited')
                    if is_auth_error(res):
                        if not reauthed and attempt < max_retries:
                            reauthed = True
                            self.connect(force=True)
                            continue
                        raise ValueError('angel_auth_failed')
                return res
            except Exception as exc:
                if str(exc) in ('angel_rate_limited', 'angel_auth_failed', 'angel_connection_error', 'angel_timeout'):
                    raise
                if is_rate_limit(exc):
                    self._last_rate_limit_time = time.monotonic()
                    logger.warning(
                        f"Angel rate limit: action={action_name} bucket={b_name} source=broker_exception attempt={attempt_num}"
                    )
                    marker = extract_rate_limit_marker(exc)
                    exc_type = type(exc).__name__
                    logger.warning(
                        f"Angel rate-limit detail: action={action_name} bucket={b_name} attempt={attempt_num} exception_type={exc_type} marker={marker}"
                    )
                    self.limiter.notify_rate_limit(self._rate_limit_cooldown_sec)
                    raise ValueError('angel_rate_limited') from None
                if is_auth_error(exc):
                    if not reauthed and attempt < max_retries:
                        reauthed = True
                        try:
                            self.connect(force=True)
                            continue
                        except Exception:
                            raise ValueError('angel_auth_failed') from None
                    raise ValueError('angel_auth_failed') from None
                if is_transient_network_error(exc):
                    if attempt < max_retries:
                        backoff = min(0.2 * (2 ** attempt), 1.0)
                        time.sleep(backoff)
                        continue
                    msg = str(exc).lower()
                    if 'timeout' in msg or 'timed out' in msg:
                        raise ValueError('angel_timeout') from None
                    raise ValueError('angel_connection_error') from None
                raise

    @staticmethod
    def _data(response):
        if not isinstance(response, dict) or response.get('status') is not True:
            raise ValueError('angel_request_failed')
        if response.get('data') is None:
            raise ValueError('angel_data_missing')
        return response['data']

    @staticmethod
    def _quote_time(raw):
        if not raw:
            raise ValueError('quote_timestamp_missing')
        try:
            stamp = datetime.fromisoformat(str(raw))
        except ValueError:
            try:
                stamp = datetime.strptime(str(raw), '%d-%b-%Y %H:%M:%S')
            except ValueError:
                raise ValueError('invalid_quote_timestamp') from None
        return stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)

    def _quote(self, quotes, instrument, now):
        matches = [q for q in quotes if str(q.get('symbolToken')) == instrument.token
                   and q.get('exchange') == instrument.exchange]
        if len(matches) != 1:
            raise ValueError('quote_identity_missing_or_ambiguous')
        quote = matches[0]
        stamp = fresh(self._quote_time(quote.get('exchFeedTime')), now, 90)
        return number(quote.get('ltp'), 'ltp', minimum=0.000001), stamp

    def fetch(self, now: datetime | None = None):
        try:
            self.connect()
            t_start_mono = time.monotonic()

            if self._custom_clock:
                req_now = timestamp(self.clock()) if now is None else timestamp(now)
            elif now is not None:
                req_now = timestamp(now)
            else:
                req_now = timestamp(self.clock())

            tokens = {}
            for item in (self.instrument, self.vix_instrument):
                tokens.setdefault(item.exchange, []).append(item.token)

            quotes_resp = self._call_with_retry(lambda: self.client.getMarketData('FULL', tokens), 'getMarketData')
            quotes = self._data(quotes_resp)
            if quotes.get('unfetched'):
                raise ValueError('quote_fetch_incomplete')

            if self._custom_clock:
                quote_now = timestamp(self.clock())
            elif now is not None and abs((timestamp(self.clock()) - timestamp(now)).total_seconds()) > 60:
                elapsed = time.monotonic() - t_start_mono
                quote_now = timestamp(now) + timedelta(seconds=elapsed)
            else:
                quote_now = timestamp(self.clock())

            price, stamp = self._quote(quotes.get('fetched', []), self.instrument, quote_now)
            vix, vix_stamp = self._quote(quotes.get('fetched', []), self.vix_instrument, quote_now)

            candles_resp = self._call_with_retry(lambda: self.client.getCandleData({
                'exchange': self.instrument.exchange, 'symboltoken': self.instrument.token,
                'interval': 'ONE_MINUTE',
                'fromdate': (req_now - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M'),
                'todate': req_now.strftime('%Y-%m-%d %H:%M'),
            }), 'getCandleData')
            candles = self._data(candles_resp)

            if self._custom_clock:
                candle_now = timestamp(self.clock())
            elif now is not None and abs((timestamp(self.clock()) - timestamp(now)).total_seconds()) > 60:
                elapsed = time.monotonic() - t_start_mono
                candle_now = timestamp(now) + timedelta(seconds=elapsed)
            else:
                candle_now = timestamp(self.clock())

            metrics = technical_snapshot(candles, candle_now)

            now_mono = time.monotonic()
            if self._cached_broker_account is not None and (now_mono - self._cached_broker_account[0] < self._broker_account_ttl_sec):
                account = self._cached_broker_account[1]
            else:
                rms_resp = self._call_with_retry(lambda: self.client.rmsLimit(), 'rmsLimit')
                positions_resp = self._call_with_retry(lambda: self.client.position(), 'position')
                book_resp = self._call_with_retry(lambda: self.client.orderBook(), 'orderBook')
                rms = self._data(rms_resp)
                positions = self._data(positions_resp)
                book = self._data(book_resp)
                if not isinstance(positions, list) or not isinstance(book, list):
                    raise ValueError('invalid_broker_account_data')
                account = {
                    'available_cash': number(rms.get('availablecash'), 'available_cash'),
                    'positions': [{key: p.get(key) for key in
                                   ('tradingsymbol', 'exchange', 'symboltoken', 'netqty', 'pnl')}
                                  for p in positions],
                    'orders': [{key: order.get(key) for key in
                                ('orderid', 'tradingsymbol', 'status', 'filledshares',
                                 'unfilledshares', 'averageprice', 'updatetime')}
                               for order in book],
                }
                self._cached_broker_account = (now_mono, account)

            return {**metrics, 'chart': candles[-80:], 'price': price, 'vix': vix,
                    'timestamp': stamp.isoformat(), 'vix_timestamp': vix_stamp.isoformat(),
                    'source': self.source, 'instrument': self.instrument.as_dict(),
                    'broker_account': account}
        except Exception as exc:
            # Only reset client and session on unrecoverable authentication errors.
            # Never wipe client on transient connection or rate-limit errors to prevent login storms.
            if is_auth_error(exc) or str(exc) == 'angel_auth_failed':
                self.connected = False
                self.client = None
            raise


class DemoFeed:
    """Explicit synthetic market data for offline UI checks, not a trading signal."""
    source = 'demo'
    connected = True

    def __init__(self):
        self.instrument = Instrument('NSE', 'DEMO-EQ', '0')

    def fetch(self, now):
        minute = now.replace(second=0, microsecond=0)
        rows = []
        for i in range(35):
            stamp = minute - timedelta(minutes=35 - i)
            price = 100 + i * 0.1
            rows.append([stamp.isoformat(), price, price + 0.2, price - 0.2, price + 0.1, 1000])
        return {**technical_snapshot(rows, now), 'chart': rows, 'price': 103.5, 'vix': 18.0,
                'timestamp': now.isoformat(), 'vix_timestamp': now.isoformat(),
                'source': 'demo', 'instrument': self.instrument.as_dict(),
                'broker_account': None}
