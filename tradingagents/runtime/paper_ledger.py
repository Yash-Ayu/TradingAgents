"""Atomic, restart-safe paper positions, fills, kill switch and audit history."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .market_snapshot import number


class PaperLedger:
    def __init__(self, path: str | Path, initial_cash: float = 100000):
        initial_cash = number(initial_cash, 'initial_cash', minimum=1)
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                instrument TEXT PRIMARY KEY, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
                average REAL NOT NULL, mark REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, dedupe TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL,
                instrument TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                qty INTEGER NOT NULL, price REAL NOT NULL, status TEXT NOT NULL, reason TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, event TEXT NOT NULL,
                details TEXT NOT NULL);
        ''')
        with self.transaction():
            for key, value in {'cash': initial_cash, 'realized_pnl': 0, 'kill_switch': False}.items():
                self.db.execute('INSERT OR IGNORE INTO state VALUES (?,?)', (key, json.dumps(value)))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def _get(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))

    def _audit(self, stamp, event, details):
        self.db.execute('INSERT INTO audit(timestamp,event,details) VALUES (?,?,?)',
                        (stamp, event, json.dumps(details, allow_nan=False)))

    def record(self, stamp, event, details):
        with self.transaction():
            self._audit(stamp, event, details)

    def bind(self, identity: str):
        with self.transaction():
            previous = self._get('identity')
            if previous and previous != identity:
                raise ValueError('ledger_belongs_to_different_instrument_or_source')
            self._set('identity', identity)

    def kill(self, enabled: bool, stamp: str):
        with self.transaction():
            self._set('kill_switch', enabled)
            self._audit(stamp, 'kill_switch', {'enabled': enabled})

    def killed(self):
        with self.lock:
            return bool(self._get('kill_switch'))

    def account(self, *, mark: tuple[str, float] | None = None, day: str | None = None):
        with self.transaction():
            if mark:
                price = number(mark[1], 'mark', minimum=0.000001)
                self.db.execute('UPDATE positions SET mark=? WHERE instrument=?', (price, mark[0]))
            rows = [dict(row) for row in self.db.execute('SELECT * FROM positions WHERE qty > 0')]
            for row in rows:
                row.update(self._get('protection:' + row['instrument'], {}) or {})
                row['unrealized_pnl'] = (row['mark'] - row['average']) * row['qty']
            cash = self._get('cash')
            equity = cash + sum(row['mark'] * row['qty'] for row in rows)
            if day and self._get('session_day') != day:
                self._set('session_day', day)
                self._set('day_start_equity', equity)
                self._set('peak_equity', equity)
            baseline = self._get('day_start_equity', equity)
            peak = max(self._get('peak_equity', equity), equity)
            self._set('peak_equity', peak)
            return {'cash': cash, 'available_margin': cash, 'equity': equity,
                    'positions': rows, 'realized_pnl': self._get('realized_pnl'),
                    'unrealized_pnl': sum(row['unrealized_pnl'] for row in rows),
                    'daily_pnl': equity - baseline,
                    'drawdown_pct': max(0, (peak - equity) / peak * 100) if peak else 0,
                    'daily_loss_pct': max(0, (baseline - equity) / baseline * 100) if baseline else 0,
                    'session_day': self._get('session_day'), 'kill_switch': self._get('kill_switch')}

    def execute(self, instrument, side, qty, price, *, dedupe: str, stamp: str, stop_loss=None, take_profit=None):
        """Cash-funded long-only fills. No network call exists in this ledger."""
        if side not in {'buy', 'sell'} or type(qty) is not int or qty <= 0:
            raise ValueError('invalid_order')
        if qty % instrument.lot_size:
            raise ValueError('quantity_not_lot_multiple')
        price = number(price, 'price', minimum=0.000001)
        protection = None
        if stop_loss is not None or take_profit is not None:
            stop_loss = number(stop_loss, 'stop_loss', minimum=0.000001)
            take_profit = number(take_profit, 'take_profit', minimum=0.000001)
            if not stop_loss < price < take_profit:
                raise ValueError('invalid_protective_levels')
            protection = {'stop_loss': stop_loss, 'take_profit': take_profit}
        instrument.validate_trade(datetime.fromisoformat(stamp))
        with self.transaction():
            existing = self.db.execute('SELECT * FROM orders WHERE dedupe=?', (dedupe,)).fetchone()
            if existing:
                return {'status': 'duplicate', 'order_id': existing['id'],
                        'fill_status': existing['status']}
            if self._get('kill_switch'):
                return {'status': 'blocked', 'reason': 'emergency_stop'}
            row = self.db.execute('SELECT * FROM positions WHERE instrument=?',
                                  (instrument.key,)).fetchone()
            held = row['qty'] if row else 0
            average = row['average'] if row else 0
            cash = self._get('cash')
            reason = None
            if side == 'buy' and qty * price > cash:
                reason = 'insufficient_paper_margin'
            if side == 'sell' and qty > held:
                reason = 'short_selling_not_supported'
            order_id = str(uuid4())
            state = 'rejected' if reason else 'filled'
            self.db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (order_id, dedupe, stamp, instrument.key, instrument.symbol,
                             side, qty, price, state, reason))
            if not reason:
                self._set('order_details:' + order_id, {
                    'lot_size': instrument.lot_size, 'lots': qty // instrument.lot_size,
                    'expiry': instrument.expiry, 'instrument_type': instrument.instrument_type,
                    'gross_realized_pnl': qty * (price - average) if side == 'sell' else None,
                    'cost_basis': average if side == 'sell' else price,
                    'charges': None, 'pnl_basis': 'before_charges',
                    'stop_loss': stop_loss, 'take_profit': take_profit})
                remaining = held + (qty if side == 'buy' else -qty)
                new_average = ((held * average + qty * price) / remaining
                               if side == 'buy' else average)
                self._set('cash', cash + (qty * price if side == 'sell' else -qty * price))
                if side == 'sell':
                    self._set('realized_pnl', self._get('realized_pnl') + qty * (price - average))
                if side == 'buy' and protection:
                    self._set('protection:' + instrument.key, protection)
                if remaining == 0:
                    self._set('protection:' + instrument.key, None)
                self.db.execute('INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?)',
                                (instrument.key, instrument.symbol, remaining, new_average, price))
            result = {'status': state, 'order_id': order_id, 'mode': 'paper',
                      'fill_status': state, 'filled_qty': 0 if reason else qty, 'reason': reason}
            self._audit(stamp, 'paper_order', {**result, 'side': side, 'qty': qty, 'price': price})
            return result

    def history(self, limit=100):
        with self.lock:
            orders = [dict(row) for row in self.db.execute(
                'SELECT * FROM orders ORDER BY rowid DESC LIMIT ?', (limit,))]
            for order in orders:
                order.update(self._get('order_details:' + order['id'], {}) or {})
            events = [dict(row) for row in self.db.execute(
                'SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))]
            for event in events:
                event['details'] = json.loads(event['details'])
            return {'orders': orders, 'events': events}

    def close(self):
        with self.lock:
            self.db.close()
