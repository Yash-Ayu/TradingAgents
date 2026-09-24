"""Bounded public research charts, separate from the execution feed."""
from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict

_CACHE = OrderedDict()
_LOCK = threading.Lock()

INTERVAL_MAP = {
    '1m': ('7d', '1m'),
    '5m': ('60d', '5m'),
    '15m': ('60d', '15m'),
    '1h': ('730d', '1h'),
    '1d': ('5y', '1d'),
}


def research_chart(symbol, interval='5m'):
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9^][A-Za-z0-9.^=-]{0,39}', symbol):
        raise ValueError('invalid_analysis_symbol')
    symbol = symbol.upper()
    interval = interval.lower() if isinstance(interval, str) else '5m'
    if interval not in INTERVAL_MAP:
        interval = '5m'
    period, yf_interval = INTERVAL_MAP[interval]

    if symbol in {'DEMO', 'DEMO-EQ'}:
        from datetime import datetime, timedelta
        from .market_snapshot import IST
        now = datetime.now(IST)
        minute = now.replace(second=0, microsecond=0)
        step_map = {'1m': 1, '5m': 5, '15m': 15, '1h': 60, '1d': 1440}
        step_minutes = step_map.get(interval, 5)
        count = 60
        rows = []
        base_price = 100.0
        for i in range(count):
            stamp = minute - timedelta(minutes=(count - i) * step_minutes)
            wave = math.sin(i * 0.3) * 1.5 + (i * 0.08)
            o = round(base_price + wave, 2)
            c = round(base_price + wave + (0.35 if (i % 3 != 0) else -0.4), 2)
            h = round(max(o, c) + 0.25, 2)
            l = round(min(o, c) - 0.20, 2)
            v = int(1200 + (i % 7) * 180 + abs(math.sin(i)) * 400)
            rows.append([stamp.isoformat(), o, h, l, c, v])
        return {
            'symbol': symbol,
            'source': 'Synthetic demo data • not live prices',
            'interval': interval,
            'chart': rows,
            'price': rows[-1][4],
            'timestamp': rows[-1][0],
            'change_pct': round((rows[-1][4] / rows[0][1] - 1) * 100, 2),
        }

    cache_key = (symbol, interval)
    with _LOCK:
        entry = _CACHE.get(cache_key)
        if entry and time.monotonic() - entry[0] < 60:
            return entry[1]

    import yfinance as yf
    candidates = [symbol]
    if symbol in {'NIFTY', 'NIFTY50', 'NIFTY-50', 'NIFTY 50', 'NIFTY_50'}:
        candidates = ['^NSEI', symbol]
    elif symbol in {'BANKNIFTY', 'NIFTYBANK', 'NIFTY BANK', 'NIFTY_BANK'}:
        candidates = ['^NSEBANK', symbol]
    elif '.' not in symbol and '^' not in symbol:
        candidates = [f"{symbol}.NS", symbol, f"{symbol}.BO"]

    frame = None
    for cand in candidates:
        try:
            df = yf.Ticker(cand).history(period=period, interval=yf_interval, auto_adjust=False, timeout=12)
            if df is not None and not df.empty:
                frame = df
                break
        except Exception:
            continue

    if frame is None or frame.empty:
        raise ValueError('market_chart_unavailable')

    rows = []
    for index, row in frame.tail(2500).iterrows():
        values = [float(row[name]) for name in ('Open', 'High', 'Low', 'Close', 'Volume')]
        if all(math.isfinite(value) for value in values) and min(values[:4]) > 0:
            rows.append([index.isoformat(), *values])
    if not rows:
        raise ValueError('market_chart_unavailable')

    result = {
        'symbol': symbol,
        'source': 'Yahoo Finance • research data, may be delayed',
        'interval': interval,
        'chart': rows,
        'price': rows[-1][4],
        'timestamp': rows[-1][0],
        'change_pct': round((rows[-1][4] / rows[0][1] - 1) * 100, 2),
    }
    with _LOCK:
        _CACHE[cache_key] = (time.monotonic(), result)
        _CACHE.move_to_end(cache_key)
        if len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    return result
