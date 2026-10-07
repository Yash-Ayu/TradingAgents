"""
Local WebSocket and REST Candle Store.
Builds and maintains completed 1-minute and 5-minute OHLCV candles from live market ticks,
caches completed bars locally for fast restart, merges with REST backfill, and guarantees
strict completed-bar semantics without forward-filling or fabricating prices.

SAFETY INVARIANTS:
1. Strictly completed bars only enter indicator and strategy calculations.
2. Never fabricate OHLC candles.
3. If data is genuinely stale, fail closed.
4. Read-only market data persistence; no order mutations.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _parse_ts(val: Any) -> Optional[datetime]:
    if isinstance(val, datetime):
        return _to_ist(val)
    if isinstance(val, str):
        try:
            # ISO format: 2026-10-07T10:30:00+05:30 or 2026-10-07 10:30
            clean = val.replace(" ", "T")
            parsed = datetime.fromisoformat(clean)
            return _to_ist(parsed)
        except Exception:
            return None
    return None


class LocalCandleStore:
    """Thread-safe store for local completed candles built from live ticks and REST backfill."""

    def __init__(self, cache_dir: Optional[Path] = None, max_bars: int = 120):
        self.cache_dir = cache_dir or Path("data_cache/candles")
        self.max_bars = max_bars
        self.lock = threading.RLock()

        # symbol -> list of completed bars: [[timestamp_iso, O, H, L, C, V], ...]
        self._bars_1m: Dict[str, List[list]] = {}
        self._bars_5m: Dict[str, List[list]] = {}

        # symbol -> current accumulating minute bar: dict(minute=dt, open=f, high=f, low=f, close=f, volume=i)
        self._curr_1m: Dict[str, dict] = {}

        # symbol -> provenance: "websocket_local", "rest_backfill", "merged"
        self._provenance: Dict[str, str] = {}
        # symbol -> last_update_mono
        self._last_update_mono: Dict[str, float] = {}

        self._ensure_cache_dir()
        self._load_from_disk()

    def _ensure_cache_dir(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.debug(f"Could not create candle cache dir {self.cache_dir}: {exc}")

    def _cache_file(self, symbol: str, interval: str) -> Path:
        clean = symbol.replace("^", "").replace(":", "_").replace("/", "_").replace(".NS", "").upper()
        return self.cache_dir / f"{clean}_{interval}.json"

    def _load_from_disk(self) -> None:
        """Load recent cached completed bars on startup."""
        with self.lock:
            if not self.cache_dir.exists():
                return
            for f in self.cache_dir.glob("*.json"):
                try:
                    parts = f.stem.rsplit("_", 1)
                    if len(parts) != 2:
                        continue
                    sym, interval = parts
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, list) and data:
                        target = self._bars_5m if interval == "5m" else self._bars_1m
                        target[sym] = data[-self.max_bars:]
                        self._provenance[sym] = "rest_backfill"
                except Exception as exc:
                    logger.debug(f"Error loading candle cache {f}: {exc}")

    def _save_to_disk(self, symbol: str, interval: str) -> None:
        """Persist rolling completed bars to disk."""
        try:
            target = self._bars_5m if interval == "5m" else self._bars_1m
            bars = target.get(symbol, [])
            if not bars:
                return
            cf = self._cache_file(symbol, interval)
            cf.write_text(json.dumps(bars[-self.max_bars:]), encoding="utf-8")
        except Exception as exc:
            logger.debug(f"Error writing candle cache for {symbol}: {exc}")

    def on_tick(self, tick: Any) -> None:
        """
        Process a real live tick from WebSocket.
        Tick must have attributes: symbol, ltp, timestamp, and optional volume.
        """
        if tick is None or not getattr(tick, "ltp", None) or tick.ltp <= 0:
            return

        with self.lock:
            symbol = getattr(tick, "symbol", "").strip().upper()
            if not symbol:
                return

            tick_time = getattr(tick, "timestamp", None) or datetime.now(IST)
            dt_ist = _to_ist(tick_time)
            bar_minute = dt_ist.replace(second=0, microsecond=0)
            ltp = float(tick.ltp)
            vol = int(getattr(tick, "volume", 0) or 0)

            curr = self._curr_1m.get(symbol)
            if curr is None:
                self._curr_1m[symbol] = {
                    "minute": bar_minute,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": vol,
                }
                self._last_update_mono[symbol] = time.monotonic()
                return

            if bar_minute > curr["minute"]:
                # The previous 1-minute bar is COMPLETED
                completed_bar = [
                    curr["minute"].isoformat(),
                    curr["open"],
                    curr["high"],
                    curr["low"],
                    curr["close"],
                    curr["volume"],
                ]
                bars_1m = self._bars_1m.setdefault(symbol, [])
                # Avoid duplicate timestamp
                if not bars_1m or bars_1m[-1][0] != completed_bar[0]:
                    bars_1m.append(completed_bar)
                    if len(bars_1m) > self.max_bars * 5:
                        self._bars_1m[symbol] = bars_1m[-self.max_bars * 5:]

                # Check 5-minute bar completion
                self._rollup_completed_5m(symbol, curr["minute"])

                # Start new accumulating 1-minute bar
                self._curr_1m[symbol] = {
                    "minute": bar_minute,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": vol,
                }
                prev_prov = self._provenance.get(symbol, "")
                self._provenance[symbol] = "merged" if "rest" in prev_prov else "websocket_local"
            elif bar_minute == curr["minute"]:
                curr["high"] = max(curr["high"], ltp)
                curr["low"] = min(curr["low"], ltp)
                curr["close"] = ltp
                curr["volume"] += vol

            self._last_update_mono[symbol] = time.monotonic()

    def _rollup_completed_5m(self, symbol: str, completed_1m_dt: datetime) -> None:
        """Derive completed 5-minute candle once its final 1-minute bar closes."""
        # 5-minute bar starting at HH:M0 ends after HH:M4 completes (i.e. at minute % 5 == 4)
        if completed_1m_dt.minute % 5 != 4:
            return

        bars_1m = self._bars_1m.get(symbol, [])
        if len(bars_1m) < 5:
            return

        # Take the last 5 1-minute bars
        recent_5 = bars_1m[-5:]
        bar_5m_start = _parse_ts(recent_5[0][0])
        if bar_5m_start is None:
            return

        o = recent_5[0][1]
        h = max(b[2] for b in recent_5)
        low = min(b[3] for b in recent_5)
        c = recent_5[-1][4]
        v = sum(b[5] for b in recent_5)

        completed_5m = [bar_5m_start.isoformat(), o, h, low, c, v]
        bars_5m = self._bars_5m.setdefault(symbol, [])
        if not bars_5m or bars_5m[-1][0] != completed_5m[0]:
            bars_5m.append(completed_5m)
            if len(bars_5m) > self.max_bars:
                self._bars_5m[symbol] = bars_5m[-self.max_bars:]
            self._save_to_disk(symbol, "5m")

    def merge_rest_candles(self, symbol: str, rest_candles: list, interval: str = "5m") -> list:
        """
        Merge raw REST historical candles with local completed bars by timestamp.
        Preserves strict chronological order, deduplicates, and limits to max_bars.
        """
        with self.lock:
            sym_key = symbol.strip().upper()
            target = self._bars_5m if interval == "5m" else self._bars_1m
            existing = target.get(sym_key, [])

            by_ts = {}
            for r in existing:
                if isinstance(r, (list, tuple)) and len(r) >= 6:
                    by_ts[str(r[0])] = list(r[:6])

            for r in rest_candles:
                if isinstance(r, (list, tuple)) and len(r) >= 6:
                    ts_key = str(r[0])
                    # Prefer locally captured completed bar if present, else insert REST bar
                    if ts_key not in by_ts:
                        by_ts[ts_key] = list(r[:6])

            merged = sorted(by_ts.values(), key=lambda b: str(b[0]))[-self.max_bars:]
            target[sym_key] = merged
            prev = self._provenance.get(sym_key, "")
            self._provenance[sym_key] = "merged" if "websocket" in prev else "rest_backfill"
            self._save_to_disk(sym_key, interval)
            return merged

    def get_candles(
        self,
        symbol: str,
        interval: str = "5m",
        min_bars: int = 25,
        max_age_seconds: float = 900.0,
        now: Optional[datetime] = None,
    ) -> Optional[dict]:
        """
        Retrieve completed candles for symbol and interval.
        Returns None if bars are insufficient (< min_bars) or stale (> max_age_seconds).
        """
        with self.lock:
            sym_key = symbol.strip().upper()
            target = self._bars_5m if interval == "5m" else self._bars_1m
            bars = target.get(sym_key, [])

            if len(bars) < min_bars:
                return None

            # Verify freshness of the latest completed bar
            last_dt = _parse_ts(bars[-1][0])
            eval_now = _to_ist(now) if now is not None else datetime.now(IST)

            if last_dt is not None:
                age_seconds = (eval_now - last_dt).total_seconds()
                # If market is active and bar is older than threshold, fail closed
                if age_seconds > max_age_seconds:
                    logger.debug(f"{sym_key} local candles stale (age={age_seconds:.0f}s > {max_age_seconds}s)")
                    return None

            prov = self._provenance.get(sym_key, "websocket_local")
            return {
                "chart": list(bars[-self.max_bars:]),
                "source": "LIVE_ANGEL_ONE",
                "provenance": prov,
                "symbol": sym_key,
                "interval": interval,
                "bar_count": len(bars),
                "latest_bar_timestamp": bars[-1][0],
            }


# Singleton accessor
_GLOBAL_CANDLE_STORE: Optional[LocalCandleStore] = None
_STORE_LOCK = threading.Lock()


def get_local_candle_store() -> LocalCandleStore:
    global _GLOBAL_CANDLE_STORE
    with _STORE_LOCK:
        if _GLOBAL_CANDLE_STORE is None:
            _GLOBAL_CANDLE_STORE = LocalCandleStore()
        return _GLOBAL_CANDLE_STORE

