"""Read-only Angel feed. This module never invokes broker order mutations."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from .market_snapshot import IST, Instrument, fresh, number, technical_snapshot


class AngelReadOnlyFeed:
    source = 'angel'

    def __init__(self, instrument: Instrument, vix_instrument: Instrument, *, client_factory=None):
        self.instrument = instrument
        self.vix_instrument = vix_instrument
        self.client_factory = client_factory
        self.client = None
        self.connected = False

    def connect(self):
        if self.client is not None:
            return
        names = ('ANGEL_API_KEY', 'ANGEL_CLIENT_ID', 'ANGEL_MPIN', 'ANGEL_TOTP_SECRET')
        values = [os.environ.get(name) for name in names]
        if not all(values):
            raise ValueError('angel_credentials_missing')
        import pyotp

        if self.client_factory is None:
            from SmartApi import SmartConnect
            factory = SmartConnect
        else:
            factory = self.client_factory
        client = factory(api_key=values[0], timeout=10)
        response = client.generateSession(values[1], values[2], pyotp.TOTP(values[3]).now())
        self._data(response)
        self.client = client
        self.connected = True

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

    def fetch(self, now: datetime):
        self.connect()
        try:
            tokens = {}
            for item in (self.instrument, self.vix_instrument):
                tokens.setdefault(item.exchange, []).append(item.token)
            quotes = self._data(self.client.getMarketData('FULL', tokens))
            if quotes.get('unfetched'):
                raise ValueError('quote_fetch_incomplete')
            price, stamp = self._quote(quotes.get('fetched', []), self.instrument, now)
            vix, vix_stamp = self._quote(quotes.get('fetched', []), self.vix_instrument, now)
            candles = self._data(self.client.getCandleData({
                'exchange': self.instrument.exchange, 'symboltoken': self.instrument.token,
                'interval': 'ONE_MINUTE',
                'fromdate': (now - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M'),
                'todate': now.strftime('%Y-%m-%d %H:%M'),
            }))
            metrics = technical_snapshot(candles, now)
            rms = self._data(self.client.rmsLimit())
            positions = self._data(self.client.position())
            book = self._data(self.client.orderBook())
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
            return {**metrics, 'chart': candles[-80:], 'price': price, 'vix': vix,
                    'timestamp': stamp.isoformat(), 'vix_timestamp': vix_stamp.isoformat(),
                    'source': self.source, 'instrument': self.instrument.as_dict(),
                    'broker_account': account}
        except Exception:
            # Next bounded scheduler attempt authenticates again. Never retry orders.
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
