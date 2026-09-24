"""
Live Market Data Provider for Angel One Indian F&O.
Supports WebSocket 2.0 streaming and REST snapshot polling with connection state management,
stale tick detection, automatic reconnect handling, and data source tagging.

ABSOLUTE SAFETY INVARIANT:
This module is strictly read-only. It only consumes market price feeds and contains
no order placement or modification logic.
"""

import logging
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from .client import AngelOneClient

logger = logging.getLogger(__name__)


class DataSource(str, Enum):
    LIVE_ANGEL_ONE = "LIVE_ANGEL_ONE"
    SIMULATION = "SIMULATION"


class ConnectionState(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"


class MarketTick(BaseModel):
    """Normalized live market tick structure."""
    symbol: str
    token: str
    exchange: str
    ltp: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    data_source: DataSource = DataSource.LIVE_ANGEL_ONE
    is_stale: bool = False


class AngelOneMarketDataProvider:
    """
    Manages live market data streams and snapshots from Angel One SmartAPI.
    Tracks connection health, automatically detects stale ticks, and manages subscriptions.
    """

    def __init__(
        self,
        client: AngelOneClient,
        max_tick_age_seconds: float = 5.0,
        data_source: DataSource = DataSource.LIVE_ANGEL_ONE,
    ):
        self.client = client
        self.max_tick_age_seconds = max_tick_age_seconds
        self.data_source = data_source
        self.state = ConnectionState.DISCONNECTED
        self.lock = threading.RLock()

        # Cache of latest ticks: token -> MarketTick
        self._latest_ticks: Dict[str, MarketTick] = {}
        # Active subscriptions: (exchange, token) -> symbol
        self._subscriptions: Dict[tuple, str] = {}
        # Callback listeners for live ticks: List[Callable[[MarketTick], None]]
        self._tick_listeners: List[Callable[[MarketTick], None]] = []

        self._ws = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5

    def is_connected(self) -> bool:
        """Check if market data stream or client session is actively connected."""
        with self.lock:
            return self.state == ConnectionState.CONNECTED

    def register_tick_listener(self, listener: Callable[[MarketTick], None]) -> None:
        """Register a callback function to receive incoming ticks."""
        with self.lock:
            self._tick_listeners.append(listener)

    def connect(self) -> bool:
        """
        Authenticate client session and initialize market data connection.
        Never logs credentials.
        """
        with self.lock:
            if not self.client.is_authenticated:
                auth_res = self.client.authenticate()
                if not auth_res.get("status"):
                    logger.error(f"MarketDataProvider failed to authenticate: {auth_res.get('message')}")
                    self.state = ConnectionState.DISCONNECTED
                    return False

            self.state = ConnectionState.CONNECTED
            self._reconnect_attempts = 0
            logger.info("MarketDataProvider connected successfully to Angel One market feed.")
            return True

    def start_websocket(self) -> bool:
        """Start official SmartWebSocketV2 background worker thread."""
        with self.lock:
            if self._ws is not None:
                return True
            if not self.client.is_authenticated:
                if not self.connect():
                    return False
            try:
                from SmartApi.smartWebSocketV2 import SmartWebSocketV2
                sws = SmartWebSocketV2(
                    auth_token=getattr(self.client, '_jwt_token', None),
                    api_key=getattr(self.client, 'api_key', None),
                    client_code=getattr(self.client, 'client_code', None),
                    feed_token=getattr(self.client, '_feed_token', None),
                )

                def on_open(wsapp):
                    logger.info("Angel One SmartWebSocketV2 connected.")
                    with self.lock:
                        self.state = ConnectionState.CONNECTED
                    self._resubscribe_all()

                def on_data(wsapp, data):
                    try:
                        raw_ltp = data.get("last_traded_price")
                        if raw_ltp is None:
                            return
                        ltp = float(raw_ltp) / 100.0
                        token = str(data.get("token", "")).strip()
                        exch_type = data.get("exchange_type", 1)
                        exchange = "NSE" if exch_type in (1, 2) else "BSE"
                        symbol = self._subscriptions.get((exchange, token), token)
                        vol = data.get("volume_trade_for_the_day")
                        self.ingest_tick(
                            symbol=symbol,
                            token=token,
                            exchange=exchange,
                            ltp=ltp,
                            volume=int(vol) if vol is not None else None,
                            timestamp=datetime.now(),
                            data_source=DataSource.LIVE_ANGEL_ONE,
                        )
                    except Exception as exc:
                        logger.error(f"Error handling WebSocket tick: {exc}")

                def on_error(wsapp, error):
                    logger.warning(f"Angel One SmartWebSocketV2 error: {error}")

                def on_close(wsapp):
                    logger.info("Angel One SmartWebSocketV2 closed.")
                    self.handle_disconnect("WebSocket closed")

                sws.on_open = on_open
                sws.on_data = on_data
                sws.on_error = on_error
                sws.on_close = on_close

                self._ws = sws
                ws_thread = threading.Thread(target=sws.connect, daemon=True)
                ws_thread.start()
                return True
            except Exception as e:
                logger.warning(f"Failed to start SmartWebSocketV2: {e}")
                return False

    def disconnect(self) -> None:
        """Disconnect market data feeds cleanly."""
        with self.lock:
            if self._ws is not None:
                try:
                    self._ws.close_connection()
                except Exception:
                    pass
                self._ws = None
            self.state = ConnectionState.DISCONNECTED
            logger.info("MarketDataProvider disconnected.")

    def handle_disconnect(self, reason: str = "Connection lost") -> None:
        """Handle unexpected stream disconnection and initiate reconnect."""
        with self.lock:
            logger.warning(f"Market data stream disconnected: {reason}. Updating state to DISCONNECTED.")
            self.state = ConnectionState.DISCONNECTED

            # Attempt auto-reconnect if under retry ceiling
            if self._reconnect_attempts < self._max_reconnect_attempts:
                self._reconnect_attempts += 1
                self.state = ConnectionState.RECONNECTING
                logger.info(f"Attempting market data reconnect ({self._reconnect_attempts}/{self._max_reconnect_attempts})...")
                # Attempt silent session recovery
                if self.connect():
                    # Resubscribe to active tokens
                    self._resubscribe_all()
                else:
                    self.state = ConnectionState.DISCONNECTED

    def subscribe(self, exchange: str, token: str, symbol: str) -> None:
        """Subscribe to live market data updates for an instrument token."""
        with self.lock:
            key = (exchange.upper(), str(token))
            self._subscriptions[key] = symbol
            logger.info(f"Subscribed to live data: {symbol} ({exchange}:{token})")
            if self._ws is not None:
                try:
                    ws_app = getattr(self._ws, 'wsapp', None)
                    sock = getattr(ws_app, 'sock', None) if ws_app else None
                    if sock and getattr(sock, 'connected', False):
                        exch_map = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}
                        exch_type = exch_map.get(exchange.upper(), 1)
                        self._ws.subscribe(f"sub_{token}", 1, [{"exchangeType": exch_type, "tokens": [str(token)]}])
                except Exception as e:
                    logger.warning(f"WebSocket subscription error for {token}: {e}")

    def unsubscribe(self, exchange: str, token: str) -> None:
        """Unsubscribe from live market data updates."""
        with self.lock:
            key = (exchange.upper(), str(token))
            if key in self._subscriptions:
                del self._subscriptions[key]
                logger.info(f"Unsubscribed from live data: {exchange}:{token}")
            if self._ws is not None:
                try:
                    exch_map = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}
                    exch_type = exch_map.get(exchange.upper(), 1)
                    self._ws.unsubscribe(f"unsub_{token}", 1, [{"exchangeType": exch_type, "tokens": [str(token)]}])
                except Exception:
                    pass

    def _resubscribe_all(self) -> None:
        """Re-register all active subscriptions after reconnect."""
        with self.lock:
            for (exch, tok), sym in list(self._subscriptions.items()):
                logger.info(f"Re-subscribing to {sym} ({exch}:{tok})")
                if self._ws is not None:
                    try:
                        exch_map = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}
                        exch_type = exch_map.get(exch.upper(), 1)
                        self._ws.subscribe(f"sub_{tok}", 1, [{"exchangeType": exch_type, "tokens": [str(tok)]}])
                    except Exception as e:
                        logger.warning(f"Error subscribing on connect for {tok}: {e}")

    def ingest_tick(
        self,
        symbol: str,
        token: str,
        exchange: str,
        ltp: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        volume: Optional[int] = None,
        open_interest: Optional[int] = None,
        timestamp: Optional[datetime] = None,
        data_source: Optional[DataSource] = None,
    ) -> MarketTick:
        """
        Process an incoming tick from WebSocket or REST poll.
        Validates timestamp freshness and notifies listeners.
        """
        now = datetime.now()
        tick_time = timestamp or now
        age = (now - tick_time).total_seconds()
        is_stale = age > self.max_tick_age_seconds

        tick = MarketTick(
            symbol=symbol,
            token=str(token),
            exchange=exchange.upper(),
            ltp=ltp,
            bid=bid,
            ask=ask,
            volume=volume,
            open_interest=open_interest,
            timestamp=tick_time,
            data_source=data_source or self.data_source,
            is_stale=is_stale,
        )

        with self.lock:
            self._latest_ticks[str(token)] = tick

        # Notify registered listeners
        for listener in self._tick_listeners:
            try:
                listener(tick)
            except Exception as e:
                logger.error(f"Error in tick listener: {e}")

        return tick

    def get_latest_tick(self, token: str) -> Optional[MarketTick]:
        """
        Retrieve latest tick for a token.
        Automatically marks is_stale=True if tick age exceeds threshold.
        """
        with self.lock:
            tick = self._latest_ticks.get(str(token))
            if not tick:
                return None

            # Re-evaluate staleness at access time
            now = datetime.now()
            age = (now - tick.timestamp).total_seconds()
            if age > self.max_tick_age_seconds:
                tick = tick.model_copy(update={"is_stale": True})

            return tick

    def poll_quote_snapshot(self, exchange: str, token: str, symbol: str) -> Optional[MarketTick]:
        """
        Fetch a fresh market quote snapshot via REST API.
        Used as reliable fallback when WebSocket is inactive or during startup.
        """
        if not self.is_connected():
            logger.warning("Cannot poll snapshot: MarketDataProvider is not connected.")
            return None

        try:
            res = self.client.get_market_data(mode="FULL", exchange_tokens={exchange: [token]})
            if not res or not res.get("status"):
                logger.warning(f"Failed to fetch market snapshot for {symbol}: {res.get('message') if res else 'No response'}")
                return None

            data_list = res.get("data", {}).get("fetched", [])
            if not data_list:
                return None

            item = data_list[0]
            ltp = float(item.get("ltp", 0.0))
            if ltp <= 0:
                return None

            bid = float(item.get("bestBidPrice", 0.0)) or None
            ask = float(item.get("bestAskPrice", 0.0)) or None
            volume = int(item.get("tradeVolume", 0)) or None
            oi = int(item.get("opnInterest", 0)) or None

            return self.ingest_tick(
                symbol=symbol,
                token=token,
                exchange=exchange,
                ltp=ltp,
                bid=bid,
                ask=ask,
                volume=volume,
                open_interest=oi,
                timestamp=datetime.now(),
                data_source=DataSource.LIVE_ANGEL_ONE,
            )
        except Exception as e:
            logger.error(f"Exception during quote snapshot poll for {symbol}: {e}")
            return None

