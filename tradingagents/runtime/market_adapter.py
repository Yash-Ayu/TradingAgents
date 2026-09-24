"""Unified Market Data Architecture for TradingAgents.

Provides clean provider adapters:
- AngelOneMarketAdapter (Official SmartAPI live quotes + historical candles)
- ZerodhaMarketAdapter (Pluggable stub conforming to interface)
- UpstoxMarketAdapter (Pluggable stub conforming to interface)
- ResearchMarketAdapter (Yahoo Finance fallback with IST normalization)
- DemoMarketAdapter (Synthetic deterministic demo feed)
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo('Asia/Kolkata')

# Built-in fast token lookup for top Indian benchmark indices and high-liquidity stocks
WELL_KNOWN_TOKENS = {
    'NIFTY': ('NSE', '99926000', 'NIFTY 50'),
    'NIFTY50': ('NSE', '99926000', 'NIFTY 50'),
    'NIFTY 50': ('NSE', '99926000', 'NIFTY 50'),
    'BANKNIFTY': ('NSE', '99926009', 'NIFTY BANK'),
    'NIFTYBANK': ('NSE', '99926009', 'NIFTY BANK'),
    'NIFTY BANK': ('NSE', '99926009', 'NIFTY BANK'),
    'FINNIFTY': ('NSE', '99926037', 'NIFTY FIN SERVICE'),
    'MIDCPNIFTY': ('NSE', '99926074', 'NIFTY MID SELECT'),
    'SENSEX': ('BSE', '99919000', 'SENSEX'),
    'SBIN': ('NSE', '3045', 'State Bank of India'),
    'SBIN-EQ': ('NSE', '3045', 'State Bank of India'),
    'SBIN.NS': ('NSE', '3045', 'State Bank of India'),
    'RELIANCE': ('NSE', '2885', 'Reliance Industries'),
    'RELIANCE-EQ': ('NSE', '2885', 'Reliance Industries'),
    'RELIANCE.NS': ('NSE', '2885', 'Reliance Industries'),
    'TCS': ('NSE', '11536', 'Tata Consultancy Services'),
    'TCS-EQ': ('NSE', '11536', 'Tata Consultancy Services'),
    'TCS.NS': ('NSE', '11536', 'Tata Consultancy Services'),
    'INFY': ('NSE', '1594', 'Infosys Ltd'),
    'INFY-EQ': ('NSE', '1594', 'Infosys Ltd'),
    'INFY.NS': ('NSE', '1594', 'Infosys Ltd'),
    'HDFCBANK': ('NSE', '1333', 'HDFC Bank Ltd'),
    'HDFCBANK-EQ': ('NSE', '1333', 'HDFC Bank Ltd'),
    'HDFCBANK.NS': ('NSE', '1333', 'HDFC Bank Ltd'),
    'ICICIBANK': ('NSE', '4963', 'ICICI Bank Ltd'),
    'ICICIBANK-EQ': ('NSE', '4963', 'ICICI Bank Ltd'),
    'ICICIBANK.NS': ('NSE', '4963', 'ICICI Bank Ltd'),
    'TATAMOTORS': ('NSE', '3456', 'Tata Motors Ltd'),
    'TATAMOTORS-EQ': ('NSE', '3456', 'Tata Motors Ltd'),
    'TATAMOTORS.NS': ('NSE', '3456', 'Tata Motors Ltd'),
    'ITC': ('NSE', '1660', 'ITC Ltd'),
    'ITC-EQ': ('NSE', '1660', 'ITC Ltd'),
    'ITC.NS': ('NSE', '1660', 'ITC Ltd'),
}


def is_indian_market_open(now: Optional[datetime] = None) -> bool:
    """Check if Indian equity/F&O market is currently open (Mon-Fri 09:15 - 15:30 IST)."""
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    else:
        current = current.astimezone(IST)
    # Weekdays: Monday is 0, Friday is 4, Saturday is 5, Sunday is 6
    if current.weekday() >= 5:
        return False
    market_open = dtime(9, 15)
    market_close = dtime(15, 30)
    return market_open <= current.time() <= market_close


class MarketDataAdapter:
    """Base interface for all market data providers."""

    name: str = 'base'

    def is_configured(self) -> bool:
        raise NotImplementedError

    def get_candles(self, symbol: str, interval: str = '5m') -> Dict[str, Any]:
        raise NotImplementedError

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        raise NotImplementedError


class AngelOneMarketAdapter(MarketDataAdapter):
    """Official Angel One SmartAPI Market Data Adapter."""

    name: str = 'angel_one'

    def __init__(self, client: Any = None):
        self._client = client
        self._provider = None
        self._lock = threading.Lock()
        self._cache: Dict[tuple, tuple[float, Dict[str, Any]]] = {}

    def _get_client(self):
        if self._client is not None:
            return self._client
        from tradingagents.integrations.angel_one.client import AngelOneClient
        client = AngelOneClient()
        if not client.is_authenticated:
            auth_res = client.authenticate()
            if not auth_res.get('status'):
                raise ValueError(f"Angel One authentication failed: {auth_res.get('message')}")
        self._client = client
        return self._client

    def _get_provider(self):
        if self._provider is not None:
            return self._provider
        from tradingagents.integrations.angel_one.market_data import AngelOneMarketDataProvider
        client = self._get_client()
        provider = AngelOneMarketDataProvider(client=client)
        provider.connect()
        provider.start_websocket()
        self._provider = provider
        return self._provider

    def is_configured(self) -> bool:
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.environ.get('ANGEL_API_KEY')
        client_code = os.environ.get('ANGEL_CLIENT_CODE') or os.environ.get('ANGEL_CLIENT_ID')
        pin = os.environ.get('ANGEL_PIN') or os.environ.get('ANGEL_MPIN')
        totp = os.environ.get('ANGEL_TOTP_SECRET')
        return bool(api_key and client_code and pin and totp)

    def resolve_token(self, symbol: str) -> tuple[str, str, str]:
        """Resolves symbol into (exchange, token, display_name)."""
        clean = symbol.strip().upper()
        if clean in {'DEMO', 'DEMO-EQ'}:
            return 'NSE', '0', 'DEMO-EQ'
        if clean in WELL_KNOWN_TOKENS:
            return WELL_KNOWN_TOKENS[clean]

        base_clean = clean.replace('.NS', '').replace('.BO', '').replace('-EQ', '')
        if base_clean in WELL_KNOWN_TOKENS:
            return WELL_KNOWN_TOKENS[base_clean]

        # Look in scrip_master cache
        scrip_file = Path('data_cache/scrip_master.json')
        if scrip_file.exists():
            try:
                with open(scrip_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for item in data:
                    sym = str(item.get('symbol', '')).upper()
                    nam = str(item.get('name', '')).upper()
                    tok = str(item.get('token', ''))
                    seg = str(item.get('exch_seg', '')).upper()
                    if sym in (clean, f"{base_clean}-EQ") and seg in ('NSE', 'BSE'):
                        return seg, tok, nam
            except Exception as e:
                logger.warning(f"Error resolving token from scrip master: {e}")

        raise ValueError(f"Could not resolve Angel One token for symbol: {symbol}")

    def get_candles(self, symbol: str, interval: str = '5m') -> Dict[str, Any]:
        clean = symbol.strip().upper()
        if clean in {'DEMO', 'DEMO-EQ'}:
            return DemoMarketAdapter().get_candles(clean, interval=interval)

        interval_map = {
            '1m': ('ONE_MINUTE', timedelta(days=15)),
            '5m': ('FIVE_MINUTE', timedelta(days=60)),
            '15m': ('FIFTEEN_MINUTE', timedelta(days=120)),
            '1h': ('ONE_HOUR', timedelta(days=365)),
            '1d': ('ONE_DAY', timedelta(days=365 * 5)),
        }
        smart_interval, delta = interval_map.get(interval.lower(), ('FIVE_MINUTE', timedelta(days=60)))
        exchange, token, name = self.resolve_token(symbol)

        cache_key = (token, interval)
        with self._lock:
            if cache_key in self._cache:
                ts, cached = self._cache[cache_key]
                if time.monotonic() - ts < 15:  # 15s cache
                    return cached

        client = self._get_client()
        now = datetime.now(IST)
        from_dt = now - delta

        # Call official Angel One SmartConnect getCandleData
        smart_connect = client._smart_connect
        params = {
            'exchange': exchange,
            'symboltoken': token,
            'interval': smart_interval,
            'fromdate': from_dt.strftime('%Y-%m-%d %H:%M'),
            'todate': now.strftime('%Y-%m-%d %H:%M'),
        }

        try:
            resp = smart_connect.getCandleData(params)
        except Exception as exc:
            logger.warning(f"Angel One candle data request failed for {symbol}: {type(exc).__name__}. Falling back to research chart.")
            return ResearchMarketAdapter().get_candles(symbol, interval=interval)

        if not resp or not resp.get('status') or not resp.get('data'):
            logger.warning(f"Angel One candle data empty for {symbol}. Falling back to research chart.")
            return ResearchMarketAdapter().get_candles(symbol, interval=interval)

        raw_rows = resp['data']
        rows = []
        for r in raw_rows[-2500:]:
            # Format: [iso_str, open, high, low, close, volume]
            rows.append([r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5]) if len(r) > 5 else 0])

        if not rows:
            raise ValueError(f"No valid candles returned for {symbol}")

        latest_price = rows[-1][4]
        first_open = rows[0][1]
        change_pct = round(((latest_price / first_open) - 1) * 100, 2) if first_open > 0 else 0.0
        is_open = is_indian_market_open(now)

        # Register token subscription for WebSocket streaming
        try:
            provider = self._get_provider()
            provider.subscribe(exchange, token, symbol)
        except Exception as e:
            logger.warning(f"Could not subscribe WebSocket for {symbol}: {e}")

        result = {
            'symbol': symbol,
            'tradingsymbol': name,
            'exchange': exchange,
            'token': token,
            'source': 'LIVE_ANGEL_ONE',
            'interval': interval,
            'is_market_open': is_open,
            'market_status': 'OPEN' if is_open else 'CLOSED',
            'chart': rows,
            'price': latest_price,
            'timestamp': rows[-1][0],
            'change_pct': change_pct,
        }

        with self._lock:
            self._cache[cache_key] = (time.monotonic(), result)
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))

        return result

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        clean = symbol.strip().upper()
        if clean in {'DEMO', 'DEMO-EQ'}:
            return DemoMarketAdapter().get_quote(clean)

        exchange, token, name = self.resolve_token(symbol)
        now = datetime.now(IST)

        # 1. Try WebSocket tick cache first
        try:
            provider = self._get_provider()
            tick = provider.get_latest_tick(token)
            if tick and not tick.is_stale and tick.ltp > 0:
                return {
                    'symbol': symbol,
                    'exchange': exchange,
                    'token': token,
                    'ltp': tick.ltp,
                    'bid': tick.bid,
                    'ask': tick.ask,
                    'volume': tick.volume or 0,
                    'source': 'LIVE_ANGEL_ONE',
                    'timestamp': tick.timestamp.isoformat(),
                    'is_market_open': is_indian_market_open(now),
                }
        except Exception:
            pass

        # 2. Fall back to REST quote snapshot
        client = self._get_client()
        resp = client.get_market_data(mode='FULL', exchange_tokens={exchange: [token]})
        if not resp or not resp.get('status'):
            raise ValueError(f"Failed to fetch Angel One quote: {resp.get('message') if resp else 'empty'}")

        items = resp.get('data', {}).get('fetched', [])
        if not items:
            raise ValueError(f"No quote data returned for {symbol}")

        item = items[0]
        return {
            'symbol': symbol,
            'exchange': exchange,
            'token': token,
            'ltp': float(item.get('ltp', 0.0)),
            'bid': float(item.get('bestBidPrice', 0.0)) or None,
            'ask': float(item.get('bestAskPrice', 0.0)) or None,
            'volume': int(item.get('tradeVolume', 0)) or 0,
            'source': 'LIVE_ANGEL_ONE',
            'timestamp': now.isoformat(),
            'is_market_open': is_indian_market_open(now),
        }


class ZerodhaMarketAdapter(MarketDataAdapter):
    """Kite Connect Market Data Adapter (Pluggable architecture)."""

    name: str = 'zerodha'

    def is_configured(self) -> bool:
        from dotenv import load_dotenv
        load_dotenv()
        return bool(os.environ.get('ZERODHA_API_KEY') and os.environ.get('ZERODHA_ACCESS_TOKEN'))

    def get_candles(self, symbol: str, interval: str = '5m') -> Dict[str, Any]:
        raise NotImplementedError("Zerodha Kite Connect credentials not configured.")

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        raise NotImplementedError("Zerodha Kite Connect credentials not configured.")


class UpstoxMarketAdapter(MarketDataAdapter):
    """Upstox OpenAPI Market Data Adapter (Pluggable architecture)."""

    name: str = 'upstox'

    def is_configured(self) -> bool:
        from dotenv import load_dotenv
        load_dotenv()
        return bool(os.environ.get('UPSTOX_API_KEY') and os.environ.get('UPSTOX_ACCESS_TOKEN'))

    def get_candles(self, symbol: str, interval: str = '5m') -> Dict[str, Any]:
        raise NotImplementedError("Upstox OpenAPI credentials not configured.")

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        raise NotImplementedError("Upstox OpenAPI credentials not configured.")


class ResearchMarketAdapter(MarketDataAdapter):
    """Fallback Market Data Adapter via Yahoo Finance with IST normalization."""

    name: str = 'research'

    def is_configured(self) -> bool:
        return True

    def get_candles(self, symbol: str, interval: str = '5m') -> Dict[str, Any]:
        from .market_view import research_chart
        chart_data = research_chart(symbol, interval=interval)
        now = datetime.now(IST)
        is_open = is_indian_market_open(now)
        chart_data['source'] = 'YAHOO_FINANCE_RESEARCH'
        chart_data['is_market_open'] = is_open
        chart_data['market_status'] = 'OPEN' if is_open else 'CLOSED'
        return chart_data

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        chart_data = self.get_candles(symbol, interval='5m')
        now = datetime.now(IST)
        return {
            'symbol': symbol,
            'ltp': chart_data.get('price', 0.0),
            'source': 'YAHOO_FINANCE_RESEARCH',
            'timestamp': chart_data.get('timestamp', now.isoformat()),
            'is_market_open': is_indian_market_open(now),
        }


class DemoMarketAdapter(MarketDataAdapter):
    """Synthetic Demo Adapter for offline testing."""

    name: str = 'demo'

    def is_configured(self) -> bool:
        return True

    def get_candles(self, symbol: str, interval: str = '5m') -> Dict[str, Any]:
        from .market_view import research_chart
        chart_data = research_chart('DEMO', interval=interval)
        chart_data['symbol'] = symbol
        chart_data['source'] = 'DEMO_SYNTHETIC'
        chart_data['is_market_open'] = False
        chart_data['market_status'] = 'CLOSED'
        return chart_data

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        now = datetime.now(IST)
        return {
            'symbol': symbol,
            'ltp': 100.0,
            'source': 'DEMO_SYNTHETIC',
            'timestamp': now.isoformat(),
            'is_market_open': False,
        }


_ACTIVE_ADAPTER: Optional[MarketDataAdapter] = None
_ADAPTER_LOCK = threading.Lock()


def get_active_market_adapter(preferred_source: Optional[str] = None) -> MarketDataAdapter:
    """Return active market data adapter based on available credentials and preference."""
    global _ACTIVE_ADAPTER
    with _ADAPTER_LOCK:
        if preferred_source == 'demo':
            return DemoMarketAdapter()
        if preferred_source == 'research':
            return ResearchMarketAdapter()

        if _ACTIVE_ADAPTER is not None and (_ACTIVE_ADAPTER.name == 'angel_one' or preferred_source is None):
            return _ACTIVE_ADAPTER

        # Check Angel One
        angel = AngelOneMarketAdapter()
        if angel.is_configured():
            try:
                # Test connectivity
                angel._get_client()
                _ACTIVE_ADAPTER = angel
                return angel
            except Exception as e:
                logger.warning(f"Angel One credentials present but init failed: {e}. Falling back to ResearchAdapter.")

        # Default fallback
        _ACTIVE_ADAPTER = ResearchMarketAdapter()
        return _ACTIVE_ADAPTER


def search_instruments(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    """Search instrument master by symbol, company name, or token."""
    q = (query or '').strip().upper()
    if not q:
        # Return default top liquid instruments
        return [
            {'symbol': 'NIFTY 50', 'tradingsymbol': 'NIFTY', 'exchange': 'NSE', 'token': '99926000', 'type': 'INDEX'},
            {'symbol': 'NIFTY BANK', 'tradingsymbol': 'BANKNIFTY', 'exchange': 'NSE', 'token': '99926009', 'type': 'INDEX'},
            {'symbol': 'SBIN-EQ', 'tradingsymbol': 'SBIN', 'exchange': 'NSE', 'token': '3045', 'name': 'STATE BANK OF INDIA', 'type': 'EQ'},
            {'symbol': 'RELIANCE-EQ', 'tradingsymbol': 'RELIANCE', 'exchange': 'NSE', 'token': '2885', 'name': 'RELIANCE INDUSTRIES', 'type': 'EQ'},
            {'symbol': 'TCS-EQ', 'tradingsymbol': 'TCS', 'exchange': 'NSE', 'token': '11536', 'name': 'TATA CONSULTANCY SERVICES', 'type': 'EQ'},
            {'symbol': 'INFY-EQ', 'tradingsymbol': 'INFY', 'exchange': 'NSE', 'token': '1594', 'name': 'INFOSYS LTD', 'type': 'EQ'},
            {'symbol': 'HDFCBANK-EQ', 'tradingsymbol': 'HDFCBANK', 'exchange': 'NSE', 'token': '1333', 'name': 'HDFC BANK LTD', 'type': 'EQ'},
            {'symbol': 'ICICIBANK-EQ', 'tradingsymbol': 'ICICIBANK', 'exchange': 'NSE', 'token': '4963', 'name': 'ICICI BANK LTD', 'type': 'EQ'},
            {'symbol': 'TATAMOTORS-EQ', 'tradingsymbol': 'TATAMOTORS', 'exchange': 'NSE', 'token': '3456', 'name': 'TATA MOTORS LTD', 'type': 'EQ'},
            {'symbol': 'ITC-EQ', 'tradingsymbol': 'ITC', 'exchange': 'NSE', 'token': '1660', 'name': 'ITC LTD', 'type': 'EQ'},
        ][:limit]

    results: List[Dict[str, Any]] = []
    seen: set = set()

    # 1. Check well-known indices and stocks first
    for sym, (exch, tok, disp) in WELL_KNOWN_TOKENS.items():
        if q in sym or q in disp.upper() or q == tok:
            key = (exch, tok)
            if key not in seen:
                seen.add(key)
                results.append({
                    'symbol': sym,
                    'tradingsymbol': sym.replace('-EQ', '').replace('.NS', ''),
                    'exchange': exch,
                    'token': tok,
                    'name': disp,
                    'type': 'INDEX' if 'NIFTY' in sym or 'SENSEX' in sym else 'EQ',
                })
                if len(results) >= limit:
                    return results

    # 2. Search scrip_master.json if available
    scrip_file = Path('data_cache/scrip_master.json')
    if scrip_file.exists():
        try:
            with open(scrip_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for item in data:
                exch = str(item.get('exch_seg', '')).upper()
                if exch not in ('NSE', 'BSE'):
                    continue
                sym = str(item.get('symbol', '')).upper()
                name = str(item.get('name', '')).upper()
                tok = str(item.get('token', ''))
                itype = str(item.get('instrumenttype', '')).upper()

                if q in sym or q in name or q == tok:
                    key = (exch, tok)
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            'symbol': sym,
                            'tradingsymbol': name,
                            'exchange': exch,
                            'token': tok,
                            'name': name,
                            'type': itype or 'EQ',
                            'lotsize': int(float(item.get('lotsize', 1))),
                        })
                        if len(results) >= limit:
                            break
        except Exception as e:
            logger.warning(f"Error during scrip master search: {e}")

    return results
