"""Centralized, thread-safe, monotonic-clock rate limiter for Angel One SmartAPI.

Enforces proactive rate limiting per endpoint category and cross-category pacing to
prevent burst violations, 429 rate limit errors, and socket drops.

Configured request limits based on official Angel One SmartAPI documentation:
- CANDLE (getCandleData): 3 req/sec maximum -> default min_interval = 0.35s (~2.85 req/sec)
- QUOTE (getMarketData / quotes): 10 req/sec maximum -> default min_interval = 0.10s (~10 req/sec)
- ACCOUNT (rmsLimit, position, orderBook, profile): 10 req/sec maximum -> default min_interval = 0.10s
- AUTH (generateSession): 1 req/5s maximum -> default min_interval = 5.0s
- GLOBAL (cross-bucket spacing): default min_interval = 0.02s (prevents 0ms socket bursts across endpoints)
- COOLDOWN (reactive backoff on HTTP 429 / exceeding access rate): default 3.0s
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from enum import Enum

from .security import is_rate_limit

logger = logging.getLogger(__name__)


class AngelBucket(str, Enum):
    """Categorized rate-limiting buckets conforming to SmartAPI endpoint limits."""
    CANDLE = "candle"
    QUOTE = "quote"
    ACCOUNT = "account"
    AUTH = "auth"


class EndpointBucket:
    """Tracks state and scheduling for a single endpoint bucket."""

    def __init__(self, name: str, min_interval: float):
        self.name = name
        self.min_interval = min_interval
        self.last_scheduled: float = 0.0


class AngelRateLimitTimeout(ValueError):
    """Raised when rate limit wait duration exceeds the bounded timeout ceiling."""
    pass


class AngelRateLimiter:
    """Thread-safe proactive rate limiter with monotonic scheduling and reactive cooldown."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        candle_min_interval: float | None = None,
        quote_min_interval: float | None = None,
        account_min_interval: float | None = None,
        auth_min_interval: float | None = None,
        global_min_interval: float | None = None,
        cooldown_sec: float | None = None,
    ):
        self.time_fn = clock or time.monotonic
        self.sleep_fn = sleep_fn or time.sleep
        self._lock = threading.Lock()

        # Load environment overrides or fallback to conservative documented defaults
        self.candle_min_interval = float(
            candle_min_interval
            if candle_min_interval is not None
            else os.environ.get("ANGEL_CANDLE_MIN_INTERVAL", 0.35)
        )
        self.quote_min_interval = float(
            quote_min_interval
            if quote_min_interval is not None
            else os.environ.get("ANGEL_QUOTE_MIN_INTERVAL", 0.10)
        )
        self.account_min_interval = float(
            account_min_interval
            if account_min_interval is not None
            else os.environ.get("ANGEL_ACCOUNT_MIN_INTERVAL", 0.10)
        )
        self.auth_min_interval = float(
            auth_min_interval
            if auth_min_interval is not None
            else os.environ.get("ANGEL_AUTH_MIN_INTERVAL", 5.0)
        )
        self.global_min_interval = float(
            global_min_interval
            if global_min_interval is not None
            else os.environ.get("ANGEL_GLOBAL_MIN_INTERVAL", 0.02)
        )
        self.cooldown_sec = float(
            cooldown_sec
            if cooldown_sec is not None
            else os.environ.get("ANGEL_RATE_LIMIT_COOLDOWN", 3.0)
        )

        self._cooldown_until: float = 0.0
        self._bucket_cooldown_until: dict[str, float] = {
            AngelBucket.CANDLE.value: 0.0,
            AngelBucket.QUOTE.value: 0.0,
            AngelBucket.ACCOUNT.value: 0.0,
            AngelBucket.AUTH.value: 0.0,
        }
        self._last_rate_limit_time: float = 0.0
        self._last_dispatch_time: float = 0.0

        self._buckets: dict[str, EndpointBucket] = {
            AngelBucket.CANDLE.value: EndpointBucket("candle", self.candle_min_interval),
            AngelBucket.QUOTE.value: EndpointBucket("quote", self.quote_min_interval),
            AngelBucket.ACCOUNT.value: EndpointBucket("account", self.account_min_interval),
            AngelBucket.AUTH.value: EndpointBucket("auth", self.auth_min_interval),
        }

    def set_clock(
        self,
        clock: Callable[[], float],
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        """Inject mock clock and sleep for deterministic testing."""
        with self._lock:
            self.time_fn = clock
            if sleep_fn is not None:
                self.sleep_fn = sleep_fn

    def acquire(
        self,
        bucket: str | AngelBucket = AngelBucket.CANDLE,
        timeout: float = 10.0,
        block_on_cooldown: bool = False,
    ) -> float:
        """Acquires permission to execute an API call in the specified bucket.

        Returns the duration waited in seconds.
        Throttles before returning.
        Raises ValueError('angel_rate_limited') if in cooldown and block_on_cooldown is False.
        Raises AngelRateLimitTimeout if required wait exceeds timeout.
        """
        b_key = bucket.value if isinstance(bucket, AngelBucket) else str(bucket).lower()
        if b_key not in self._buckets:
            b_key = AngelBucket.CANDLE.value

        with self._lock:
            now = self.time_fn()

            # 1. Reactive cooldown handling
            effective_cooldown = max(
                self._cooldown_until,
                self._bucket_cooldown_until.get(b_key, 0.0),
            )
            if now < effective_cooldown:
                if not block_on_cooldown:
                    logger.warning(
                        f"Angel rate limit: action=acquire bucket={b_key} source=proactive_cooldown attempt=0"
                    )
                    raise ValueError("angel_rate_limited")
                cooldown_wait = effective_cooldown - now
                if cooldown_wait > timeout:
                    logger.warning(
                        f"Angel rate limit: action=acquire bucket={b_key} source=proactive_timeout attempt=0"
                    )
                    raise AngelRateLimitTimeout(
                        f"angel_rate_limited: cooldown wait {cooldown_wait:.2f}s exceeds timeout {timeout:.2f}s"
                    )

            target_bucket = self._buckets[b_key]

            # 2. Determine earliest scheduled slot for this bucket
            earliest_bucket = max(now, target_bucket.last_scheduled + target_bucket.min_interval)

            # 3. Global anti-burst spacing (ensures rapid calls across different buckets don't burst at 0ms)
            earliest_global = max(now, self._last_dispatch_time + self.global_min_interval)

            # 4. Respect cooldown if active
            scheduled_time = max(earliest_bucket, earliest_global, effective_cooldown)

            wait_duration = scheduled_time - now
            if wait_duration > timeout:
                logger.warning(
                    f"Angel rate limit: action=acquire bucket={b_key} source=proactive_timeout attempt=0"
                )
                raise AngelRateLimitTimeout(
                    f"angel_rate_limited: wait duration {wait_duration:.2f}s exceeds timeout {timeout:.2f}s for bucket {b_key}"
                )

            # 5. Reserve slot
            target_bucket.last_scheduled = scheduled_time
            self._last_dispatch_time = max(self._last_dispatch_time + self.global_min_interval, now)

        # 6. Throttle OUTSIDE the lock
        if wait_duration > 0:
            self.sleep_fn(wait_duration)

        return max(0.0, wait_duration)

    def notify_rate_limit(
        self,
        cooldown_sec: float | None = None,
        action: str | None = None,
        bucket: str | AngelBucket | None = None,
        source: str | None = None,
        attempt: int | None = None,
    ) -> None:
        """Notifies the limiter that upstream Angel One returned a rate limit response (429 / AB1004).

        Activates reactive cooldown for the supplied bucket.
        If no bucket is supplied, preserves the original global fail-safe cooldown.
        """
        cd = cooldown_sec if cooldown_sec is not None else self.cooldown_sec
        with self._lock:
            now = self.time_fn()
            if bucket is None:
                self._cooldown_until = max(self._cooldown_until, now + cd)
            else:
                b_str = bucket.value if hasattr(bucket, "value") else str(bucket).lower()
                if b_str not in self._bucket_cooldown_until:
                    self._cooldown_until = max(self._cooldown_until, now + cd)
                else:
                    self._bucket_cooldown_until[b_str] = max(
                        self._bucket_cooldown_until[b_str], now + cd
                    )
            self._last_rate_limit_time = now
            if action and bucket and source:
                b_str = bucket.value if hasattr(bucket, "value") else str(bucket)
                att_str = attempt if attempt is not None else 1
                logger.warning(
                    f"Angel rate limit: action={action} bucket={b_str} source={source} attempt={att_str}"
                )
            logger.warning(f"Angel One rate limit encountered. Cooldown active for {cd:.1f}s.")

    def is_in_cooldown(self, bucket: str | AngelBucket | None = None) -> bool:
        """Check global cooldown or the supplied endpoint bucket cooldown."""
        with self._lock:
            now = self.time_fn()
            if bucket is None:
                return now < self._cooldown_until
            b_key = bucket.value if isinstance(bucket, AngelBucket) else str(bucket).lower()
            return now < max(
                self._cooldown_until,
                self._bucket_cooldown_until.get(b_key, 0.0),
            )

    def remaining_cooldown(self) -> float:
        """Get remaining cooldown duration in seconds."""
        with self._lock:
            now = self.time_fn()
            return max(0.0, self._cooldown_until - now)

    def reset(self) -> None:
        """Reset all bucket schedules and cooldown (primarily for test teardown)."""
        with self._lock:
            self._cooldown_until = 0.0
            for key in self._bucket_cooldown_until:
                self._bucket_cooldown_until[key] = 0.0
            self._last_rate_limit_time = 0.0
            self._last_dispatch_time = 0.0
            for b in self._buckets.values():
                b.last_scheduled = 0.0

    @contextmanager
    def throttle(
        self,
        bucket: str | AngelBucket = AngelBucket.CANDLE,
        timeout: float = 10.0,
        block_on_cooldown: bool = False,
    ):
        """Context manager to throttle before executing block and capture rate limits."""
        self.acquire(bucket, timeout=timeout, block_on_cooldown=block_on_cooldown)
        try:
            yield
        except Exception as exc:
            if is_rate_limit(exc):
                b_str = bucket.value if hasattr(bucket, "value") else str(bucket)
                logger.warning(
                    f"Angel rate limit: action=throttle bucket={b_str} source=broker_exception attempt=1"
                )
                self.notify_rate_limit()
            raise


_DEFAULT_LIMITER: AngelRateLimiter | None = None
_LIMITER_LOCK = threading.Lock()


def get_angel_rate_limiter() -> AngelRateLimiter:
    """Retrieve the centralized singleton rate limiter for Angel One requests."""
    global _DEFAULT_LIMITER
    with _LIMITER_LOCK:
        if _DEFAULT_LIMITER is None:
            _DEFAULT_LIMITER = AngelRateLimiter()
        return _DEFAULT_LIMITER


def set_angel_rate_limiter(limiter: AngelRateLimiter | None) -> None:
    """Set or reset the centralized singleton rate limiter."""
    global _DEFAULT_LIMITER
    with _LIMITER_LOCK:
        _DEFAULT_LIMITER = limiter
