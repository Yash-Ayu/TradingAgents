"""Validated market snapshots shared by read-only feeds and the paper runner."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def number(value, name: str, *, minimum: float = 0) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'invalid_{name}') from None
    if isinstance(value, bool) or not math.isfinite(result) or result < minimum:
        raise ValueError(f'invalid_{name}')
    return result


def timestamp(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except ValueError:
            raise ValueError('invalid_timestamp') from None
    if result.tzinfo is None:
        raise ValueError('timestamp_timezone_required')
    return result.astimezone(IST)


def fresh(value, now: datetime, max_age: float) -> datetime:
    parsed = timestamp(value)
    age = (timestamp(now) - parsed).total_seconds()
    if age < -5 or age > max_age:
        raise ValueError('stale_or_future_market_data')
    return parsed


@dataclass(frozen=True)
class Instrument:
    exchange: str
    symbol: str
    token: str
    lot_size: int = 1
    instrument_type: str = 'EQ'
    expiry: str | None = None

    def __post_init__(self):
        if self.exchange not in {'NSE', 'BSE', 'NFO', 'BFO'}:
            raise ValueError('unsupported_exchange')
        if not self.symbol or not self.token or not self.token.isdigit():
            raise ValueError('invalid_instrument_identity')
        if type(self.lot_size) is not int or self.lot_size < 1:
            raise ValueError('invalid_lot_size')

    @property
    def key(self):
        return f'{self.exchange}:{self.token}'

    def validate_trade(self, now: datetime):
        if self.instrument_type not in {'EQ', 'FUTIDX', 'FUTSTK', 'OPTIDX', 'OPTSTK'}:
            raise ValueError('non_tradable_instrument')
        if self.exchange in {'NFO', 'BFO'} and not self.expiry:
            raise ValueError('expiry_required')
        if self.expiry and date.fromisoformat(self.expiry) < timestamp(now).date():
            raise ValueError('expired_instrument')

    def as_dict(self):
        return asdict(self)


class InstrumentResolver:
    """Exact master-data matches only; never guess between expiry/strike variants."""
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def resolve(self, exchange: str, symbol: str, *, token: str | None = None) -> Instrument:
        matches = [row for row in self.rows
                   if row.get('exch_seg') == exchange and row.get('symbol') == symbol
                   and (token is None or str(row.get('token')) == token)]
        if len(matches) != 1:
            raise ValueError('instrument_missing_or_ambiguous')
        row = matches[0]
        expiry = row.get('expiry')
        if expiry:
            try:
                expiry = datetime.strptime(expiry, '%d%b%Y').date().isoformat()
            except ValueError:
                expiry = date.fromisoformat(expiry).isoformat()
        kind = row.get('instrumenttype') or ('EQ' if symbol.endswith('-EQ') else 'INDEX')
        return Instrument(exchange, symbol, str(row['token']), int(row['lotsize']), kind, expiry)


def technical_snapshot(rows: list, now: datetime) -> dict:
    """14-bar mean true range; ATR ratio versus preceding 14 bars; trend efficiency.

    Bars are one-minute OHLCV, timestamped at their start. Only completed bars
    may influence an order. These deterministic metrics are not strategy signals.
    """
    bars = []
    for row in rows:
        if len(row) != 6:
            raise ValueError('invalid_candle')
        stamp = timestamp(row[0])
        o, h, low, c = [number(v, 'candle_price', minimum=0.000001) for v in row[1:5]]
        number(row[5], 'volume')
        if low > min(o, c) or h < max(o, c) or h < low:
            raise ValueError('invalid_ohlc')
        if stamp > now:
            raise ValueError('future_candle')
        if stamp + timedelta(minutes=1) <= now:
            bars.append((stamp, h, low, c))
    if len(bars) < 29:
        raise ValueError('insufficient_completed_candles')
    if any(a[0] >= b[0] for a, b in zip(bars, bars[1:], strict=False)):
        raise ValueError('candles_not_strictly_ordered')
    bars = bars[-29:]
    if any((b[0] - a[0]).total_seconds() != 60 for a, b in zip(bars, bars[1:], strict=False)):
        raise ValueError('candle_gap')
    fresh(bars[-1][0] + timedelta(minutes=1), now, 180)
    ranges = [max(b[1] - b[2], abs(b[1] - a[3]), abs(b[2] - a[3]))
              for a, b in zip(bars, bars[1:], strict=False)]
    baseline = sum(ranges[:14]) / 14
    atr = sum(ranges[-14:]) / 14
    if baseline <= 0 or atr <= 0:
        raise ValueError('invalid_atr')
    recent = bars[-15:]
    travel = sum(abs(b[3] - a[3]) for a, b in zip(recent, recent[1:], strict=False))
    trend = abs(recent[-1][3] - recent[0][3]) / travel if travel else 0.0
    return {'atr': atr, 'atr_ratio': atr / baseline, 'trend_strength': min(trend, 1.0),
            'bar_timestamp': bars[-1][0].isoformat()}


class SessionCalendar:
    """Explicit per-year exchange calendar, including supplied special sessions."""
    def __init__(self, data: dict | None = None):
        from .market_session import MarketSessionMonitor
        self.data = data or {}
        holidays = {date.fromisoformat(day) for day in self.data.get('holidays', [])}
        self.monitor = MarketSessionMonitor(holidays=holidays)

    def bounds(self, now: datetime):
        from datetime import time
        local = timestamp(now)
        if self.data.get('year') != local.year:
            raise ValueError('current_year_exchange_calendar_required')
        special = self.data.get('special_sessions', {}).get(local.date().isoformat())
        if special:
            opening, closing = (time.fromisoformat(special[key]) for key in ('open', 'close'))
            if opening >= closing:
                raise ValueError('invalid_special_session')
        else:
            if local.weekday() >= 5 or local.date() in self.monitor.holidays:
                return None
            opening, closing = self.monitor.market_hours['india_nse'].values()
        return (datetime.combine(local.date(), opening, IST),
                datetime.combine(local.date(), closing, IST))

    def is_open(self, now):
        bounds = self.bounds(now)
        return bool(bounds and bounds[0] <= now < bounds[1])
