"""
DhanHQ Market Data Provider (Read-Only).
Integrates with DhanHQ API for quotes, LTP, historical candles, and live WebSocket streaming.

STRICT SAFETY INVARIANTS:
1. Purely read-only market data; absolutely NO order placement or mutation APIs.
2. Credentials and access tokens are managed by DhanTokenManager and redacted.
3. Live WebSocket ticks stream directly into LocalCandleStore for completed bar synthesis.
4. If credentials are not present, returns not_configured cleanly without crashing.
"""

from __future__ import annotations

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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from tradingagents.runtime.security import register_secret
from .auth import DhanAuthState, DhanTokenManager, get_dhan_token_manager

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class DhanMarketDataState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    NOT_CONFIGURED = "not_configured"


# Well-known Dhan securityId mappings
WELL_KNOWN_DHAN_SECS = {
    "NIFTY": ("IDX_I", "13", "INDEX"),
    "NIFTY 50": ("IDX_I", "13", "INDEX"),
    "BANKNIFTY": ("IDX_I", "25", "INDEX"),
    "NIFTY BANK": ("IDX_I", "25", "INDEX"),
    "FINNIFTY": ("IDX_I", "27", "INDEX"),
    "MIDCPNIFTY": ("IDX_I", "44", "INDEX"),
    "SENSEX": ("IDX_I", "51", "INDEX"),
    "RELIANCE": ("NSE_EQ", "2885", "EQUITY"),
    "TCS": ("NSE_EQ", "11536", "EQUITY"),
    "HDFCBANK": ("NSE_EQ", "1333", "EQUITY"),
    "INFY": ("NSE_EQ", "1594", "EQUITY"),
    "ICICIBANK": ("NSE_EQ", "4963", "EQUITY"),
    "SBIN": ("NSE_EQ", "3045", "EQUITY"),
    "BHARTIARTL": ("NSE_EQ", "10604", "EQUITY"),
    "TATAMOTORS": ("NSE_EQ", "3456", "EQUITY"),
    "ITC": ("NSE_EQ", "1660", "EQUITY"),
    "KOTAKBANK": ("NSE_EQ", "1922", "EQUITY"),
    "LT": ("NSE_EQ", "11483", "EQUITY"),
    "AXISBANK": ("NSE_EQ", "5900", "EQUITY"),
}


class DhanMarketDataProvider:
    """
    Read-only market data provider for DhanHQ.
    Provides quotes, candles, and live tick streams without broker mutation capability.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        access_token: Optional[str] = None,
        token_manager: Optional[DhanTokenManager] = None,
        base_url: str = "https://api.dhan.co/v2",
    ):
        self.base_url = base_url
        if token_manager is not None:
            self.token_manager = token_manager
        else:
            self.token_manager = DhanTokenManager(
                client_id=client_id,
                access_token=access_token,
            )
        self._client_id = (
            client_id or getattr(self.token_manager, "_client_id", "") or os.environ.get("DHAN_CLIENT_ID", "")
        ).strip()

        self._state: DhanMarketDataState = (
            DhanMarketDataState.HEALTHY if self.is_configured() else DhanMarketDataState.NOT_CONFIGURED
        )
        self._last_error: Optional[str] = None
        self.last_successful_timestamp: Optional[str] = None

        if self._client_id:
            register_secret(self._client_id)

        self._ws_feed: Optional[DhanWebSocketFeed] = None
        self._tick_listeners: List[Callable[[Any], None]] = []

    def is_configured(self) -> bool:
        """Check if required Dhan credentials exist in configuration/environment."""
        return bool(self._client_id and self.token_manager.is_configured())

    @property
    def state(self) -> DhanMarketDataState:
        if not self.is_configured():
            return DhanMarketDataState.NOT_CONFIGURED
        return self._state

    def get_headers(self) -> Dict[str, str]:
        """HTTP headers for Dhan API without logging credentials."""
        if not self.is_configured():
            return {}
        try:
            token = self.token_manager.get_valid_access_token()
        except Exception as exc:
            self._last_error = f"auth_token_error: {exc}"
            self._state = DhanMarketDataState.DEGRADED
            return {}

        return {
            "access-token": token,
            "client-id": self._client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def register_tick_listener(self, listener: Callable[[Any], None]) -> None:
        """Register a callback for incoming live WebSocket ticks."""
        self._tick_listeners.append(listener)
        if self._ws_feed is not None:
            self._ws_feed.register_tick_listener(listener)

    def start_websocket(self) -> None:
        """Start background Dhan WebSocket feed if configured."""
        if not self.is_configured():
            return
        if self._ws_feed is None:
            self._ws_feed = DhanWebSocketFeed(
                token_manager=self.token_manager,
                client_id=self._client_id,
            )
            for listener in self._tick_listeners:
                self._ws_feed.register_tick_listener(listener)
            try:
                from tradingagents.runtime.local_candle_store import get_local_candle_store
                self._ws_feed.register_tick_listener(get_local_candle_store().on_tick)
            except Exception as exc:
                logger.debug(f"Could not attach local candle store to Dhan WebSocket: {exc}")
        self._ws_feed.connect()

    def get_quote(
        self, symbol: str, security_id: Optional[str] = None, exchange_segment: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch quote / LTP for symbol.
        Returns normalized quote dict or None if not configured/failed.
        """
        if not self.is_configured():
            self._state = DhanMarketDataState.NOT_CONFIGURED
            return None

        clean = symbol.strip().upper()
        sec_info = self.resolve_instrument_info(clean)
        sec_id = security_id or (sec_info[1] if sec_info else None)
        seg = exchange_segment or (sec_info[0] if sec_info else "NSE_EQ")

        if not sec_id:
            logger.debug(f"Could not resolve Dhan security ID for {clean}")
            return None

        headers = self.get_headers()
        if not headers:
            return None

        url = f"{self.base_url}/marketfeed/quote"
        payload = json.dumps({
            seg: [int(sec_id) if sec_id.isdigit() else sec_id]
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                quotes = data.get("data", {})
                segment_quotes = quotes.get(seg, {}) if isinstance(quotes, dict) else {}
                item = segment_quotes.get(str(sec_id)) or {}
                ltp = float(item.get("last_price", 0.0) or 0.0)
                if ltp > 0:
                    self._state = DhanMarketDataState.HEALTHY
                    now_iso = datetime.now(IST).isoformat()
                    self.last_successful_timestamp = now_iso
                    return {
                        "symbol": clean,
                        "exchange": "NSE",
                        "token": str(sec_id),
                        "ltp": ltp,
                        "bid": float(item.get("buy_price", 0.0) or 0.0),
                        "ask": float(item.get("sell_price", 0.0) or 0.0),
                        "volume": int(item.get("volume", 0) or 0),
                        "source": "LIVE_DHAN",
                        "timestamp": now_iso,
                        "is_stale": False,
                    }
        except urllib.error.HTTPError as exc:
            self._last_error = f"http_{exc.code}"
            if exc.code in (401, 403):
                self.token_manager.mark_token_invalid(f"quote_http_{exc.code}")
                self._state = DhanMarketDataState.DEGRADED
            elif exc.code == 429:
                self._state = DhanMarketDataState.RATE_LIMITED
            else:
                self._state = DhanMarketDataState.DEGRADED
            logger.debug(f"Dhan get_quote failed for {clean}: {self._last_error}")
        except Exception as exc:
            self._last_error = str(exc)
            if "429" in str(exc):
                self._state = DhanMarketDataState.RATE_LIMITED
            elif "401" in str(exc) or "403" in str(exc):
                self.token_manager.mark_token_invalid("quote_unauthorized")
                self._state = DhanMarketDataState.DEGRADED
            else:
                self._state = DhanMarketDataState.DEGRADED
            logger.debug(f"Dhan get_quote failed for {clean}: {exc}")

        return None

    def get_candles(
        self,
        symbol: str,
        interval: str = "5m",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch historical / intraday completed candles from Dhan.
        Returns dict with chart rows [[ts, o, h, l, c, v], ...] or None if unavailable.
        """
        if not self.is_configured():
            self._state = DhanMarketDataState.NOT_CONFIGURED
            return None

        clean = symbol.strip().upper()
        sec_info = self.resolve_instrument_info(clean)
        if not sec_info:
            return None

        seg, sec_id, inst = sec_info
        headers = self.get_headers()
        if not headers:
            return None

        # Map interval: "5" or "1"
        dhan_interval = "5" if interval == "5m" else "1"
        now = datetime.now(IST)
        to_str = to_date or now.strftime("%Y-%m-%d")
        from_str = from_date or (now - timedelta(days=5)).strftime("%Y-%m-%d")

        url = f"{self.base_url}/charts/intraday"
        payload = json.dumps({
            "securityId": str(sec_id),
            "exchangeSegment": seg,
            "instrument": inst,
            "interval": dhan_interval,
            "fromDate": from_str,
            "toDate": to_str,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ts_list = data.get("timestamp", [])
                o_list = data.get("open", [])
                h_list = data.get("high", [])
                l_list = data.get("low", [])
                c_list = data.get("close", [])
                v_list = data.get("volume", [])

                if ts_list and len(ts_list) >= 25:
                    rows = []
                    for i in range(len(ts_list)):
                        raw_ts = ts_list[i]
                        if isinstance(raw_ts, (int, float)):
                            dt_str = datetime.fromtimestamp(raw_ts, tz=IST).isoformat()
                        else:
                            dt_str = str(raw_ts)
                        rows.append([
                            dt_str,
                            float(o_list[i]),
                            float(h_list[i]),
                            float(l_list[i]),
                            float(c_list[i]),
                            int(v_list[i]) if i < len(v_list) else 0,
                        ])
                    self._state = DhanMarketDataState.HEALTHY
                    self.last_successful_timestamp = rows[-1][0]
                    return {
                        "symbol": clean,
                        "source": "LIVE_DHAN",
                        "chart": rows,
                        "interval": interval,
                        "price": float(rows[-1][4]),
                        "timestamp": rows[-1][0],
                    }
        except urllib.error.HTTPError as exc:
            self._last_error = f"http_{exc.code}"
            if exc.code in (401, 403):
                self.token_manager.mark_token_invalid(f"candles_http_{exc.code}")
                self._state = DhanMarketDataState.DEGRADED
            elif exc.code == 429:
                self._state = DhanMarketDataState.RATE_LIMITED
            else:
                self._state = DhanMarketDataState.DEGRADED
            logger.debug(f"Dhan get_candles failed for {clean}: {self._last_error}")
        except Exception as exc:
            self._last_error = str(exc)
            if "429" in str(exc):
                self._state = DhanMarketDataState.RATE_LIMITED
            elif "401" in str(exc) or "403" in str(exc):
                self.token_manager.mark_token_invalid("candles_unauthorized")
                self._state = DhanMarketDataState.DEGRADED
            else:
                self._state = DhanMarketDataState.DEGRADED
            logger.debug(f"Dhan get_candles failed for {clean}: {exc}")

        return None

    def resolve_instrument_info(self, symbol: str) -> Optional[Tuple[str, str, str]]:
        """
        Map symbol to (exchangeSegment, securityId, instrumentType).
        Returns None if symbol cannot be resolved.
        """
        clean = symbol.replace(".NS", "").replace("-EQ", "").strip().upper()
        if clean in WELL_KNOWN_DHAN_SECS:
            return WELL_KNOWN_DHAN_SECS[clean]

        # Options check: e.g. NIFTY26OCT25000CE or RELIANCE26OCT2500PE
        if clean.endswith("CE") or clean.endswith("PE"):
            inst_type = "OPTIDX" if any(idx in clean for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY")) else "OPTSTK"
            # Try to resolve securityId from numeric suffix or scrip cache if present
            return ("NSE_FNO", clean, inst_type)

        # Default fallback for NSE equity if numeric or recognized
        if clean.isdigit():
            return ("NSE_EQ", clean, "EQUITY")

        return None

    def resolve_security_id(self, symbol: str) -> Optional[str]:
        """Convenience method returning securityId."""
        info = self.resolve_instrument_info(symbol)
        return info[1] if info else None

    def get_status(self) -> Dict[str, Any]:
        """Observability telemetry without sensitive credentials."""
        return {
            "configured": self.is_configured(),
            "state": self.state.value,
            "last_error": self._last_error,
            "last_successful_timestamp": self.last_successful_timestamp,
            "token_status": self.token_manager.get_status(),
        }


class DhanWebSocketFeed:
    """
    Live WebSocket feed client for DhanHQ (wss://api-feed.dhan.co).
    Decodes binary tick packets into normalized MarketTicks and dispatches to listeners.
    Strictly read-only; no order placement or mutation capabilities.
    """

    def __init__(
        self,
        token_manager: DhanTokenManager,
        client_id: str,
        feed_url: str = "wss://api-feed.dhan.co",
    ):
        self.token_manager = token_manager
        self.client_id = client_id
        self.feed_url = feed_url
        self._listeners: List[Callable[[Any], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.last_tick_time: Optional[datetime] = None
        self.connected = False

    def register_tick_listener(self, listener: Callable[[Any], None]) -> None:
        """Register a callback to receive parsed ticks."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def connect(self) -> None:
        """Start WebSocket streaming in a background daemon thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, name="dhan-ws-feed", daemon=True)
            self._thread.start()

    def disconnect(self) -> None:
        """Stop WebSocket streaming."""
        with self._lock:
            self._running = False
            self.connected = False

    def _run_loop(self) -> None:
        """Daemon worker loop connecting to Dhan WebSocket."""
        backoff = 2.0
        while self._running:
            try:
                token = self.token_manager.get_valid_access_token()
                if not token:
                    time.sleep(5.0)
                    continue

                import websockets.sync.client as ws_client

                # URL with authentication query params
                ws_url = (
                    f"{self.feed_url}?version=2"
                    f"&token={urllib.parse.quote(token)}"
                    f"&clientId={urllib.parse.quote(self.client_id)}"
                    f"&authType=2"
                )

                logger.info("Connecting to Dhan WebSocket feed...")
                with ws_client.connect(ws_url, open_timeout=10, close_timeout=5) as ws:
                    self.connected = True
                    backoff = 2.0
                    logger.info("Dhan WebSocket feed connected.")

                    while self._running:
                        msg = ws.recv(timeout=1.0)
                        if isinstance(msg, bytes):
                            self._handle_binary_message(msg)

            except Exception as exc:
                self.connected = False
                if not self._running:
                    break
                logger.debug(f"Dhan WebSocket connection error: {exc}. Reconnecting in {backoff:.0f}s...")
                time.sleep(backoff)
                backoff = min(60.0, backoff * 1.5)

    def _handle_binary_message(self, data: bytes) -> None:
        """
        Parse binary tick packet from Dhan Live Market Feed.
        Unpacks ResponseCode, SecurityId, LTP, Volume, and Timestamp.
        """
        if len(data) < 8:
            return

        try:
            # First byte: Response Code
            resp_code = data[0]
            # Offsets vary by response packet type:
            # 2: Ticker packet (8 bytes payload)
            # 4: Quote packet (42 bytes payload)
            # 8: Full packet
            if resp_code in (1, 2, 4, 8) and len(data) >= 16:
                # Security ID at bytes 4..8 (uint32 little-endian)
                sec_id = struct.unpack("<I", data[4:8])[0]
                # LTP float32 little-endian at bytes 8..12
                ltp = float(struct.unpack("<f", data[8:12])[0])

                vol = 0
                if len(data) >= 20 and resp_code in (4, 8):
                    vol = int(struct.unpack("<I", data[16:20])[0])

                if ltp > 0:
                    now = datetime.now(IST)
                    self.last_tick_time = now

                    # Create tick payload compatible with LocalCandleStore
                    class _DhanTick:
                        def __init__(self, token_str: str, price: float, volume: int, ts: datetime):
                            self.symbol = f"DHAN_{token_str}"
                            self.token = token_str
                            self.ltp = price
                            self.volume = volume
                            self.timestamp = ts
                            self.is_stale = False
                            self.data_source = "LIVE_DHAN"

                    tick = _DhanTick(str(sec_id), ltp, vol, now)
                    with self._lock:
                        listeners = list(self._listeners)
                    for l in listeners:
                        try:
                            l(tick)
                        except Exception as exc:
                            logger.debug(f"Error in Dhan tick listener: {exc}")

        except Exception as exc:
            logger.debug(f"Failed to unpack Dhan binary packet: {exc}")
