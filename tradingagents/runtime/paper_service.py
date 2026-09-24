"""One paper-only application owning feed, risk, engine, ledger and scheduler."""
from __future__ import annotations

import copy
import math
import re
import threading
from datetime import datetime, timedelta

from .ai_analysis import AIAnalysis
from .market_snapshot import IST, SessionCalendar, fresh, number, timestamp
from .safe_runtime import RiskGate


class PaperTradingService:
    def __init__(self, feed, ledger, *, graph_factory=None, analysis_symbol=None,
                 calendar=None, interval=60, max_errors=3, max_order_value=10000,
                 max_order_qty=50, risk_fraction=0.01, daily_loss_limit=3.0, clock=None):
        self.feed, self.ledger = feed, ledger
        self.instrument = feed.instrument
        self.graph_factory = graph_factory
        self.graph = None
        self.ai = AIAnalysis()
        self.auto_enabled = False
        self.last_ai_consumed = None
        self.last_analysis_bar = None
        self.analysis_symbol = analysis_symbol
        self.calendar = calendar or SessionCalendar()
        self.interval = number(interval, 'interval', minimum=1)
        if type(max_errors) is not int or max_errors < 1:
            raise ValueError('invalid_max_errors')
        if type(max_order_qty) is not int or max_order_qty < 1:
            raise ValueError('invalid_max_order_qty')
        self.max_errors = max_errors
        self.max_order_qty = max_order_qty
        self.max_order_value = number(max_order_value, 'max_order_value', minimum=1)
        self.risk_fraction = number(risk_fraction, 'risk_fraction', minimum=0.000001)
        if self.risk_fraction > 0.1:
            raise ValueError('risk_fraction_too_large')
        self.daily_loss_limit = number(daily_loss_limit, 'daily_loss_limit', minimum=0.01)
        self.clock = clock or (lambda: datetime.now(IST))
        self.gate = RiskGate()
        self.lock = threading.RLock()
        self.cycle_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
        self.generation = 0
        self.errors = 0
        self.last_error = None
        self.last_snapshot = None
        self.last_result = {'status': 'stopped'}
        self.last_decision = None
        self.last_risk = {'risk_state': 'unknown', 'allow_trade': False, 'reasons': ['No market data']}
        self.ledger.bind(f'{self.feed.source}:{self.instrument.key}')

    def _stamp(self):
        return timestamp(self.clock()).isoformat()

    def start(self, *, background=True):
        with self.lock:
            if self.ledger.killed():
                raise ValueError('emergency_stop_latched')
            if self.feed.source != 'demo':
                self.calendar.bounds(timestamp(self.clock()))
            if self.running:
                return self.status()
            if self.thread and self.thread.is_alive():
                raise ValueError('previous_cycle_still_stopping')
            self.running = True
            self.generation += 1
            self.errors = 0
            self.last_error = None
            self.stop_event.clear()
            self.ledger.record(self._stamp(), 'started', {'source': self.feed.source, 'mode': 'paper'})
            if background:
                self.thread = threading.Thread(target=self._run, name='paper-scheduler', daemon=True)
                self.thread.start()
        return self.status()

    def stop(self):
        with self.lock:
            self.auto_enabled = False
            self.ai.set_auto(False)
            self.running = False
            self.generation += 1
            self.stop_event.set()
            self.ledger.record(self._stamp(), 'stopped', {})
        return self.status()

    def emergency_stop(self):
        with self.lock:
            self.auto_enabled = False
            self.ai.set_auto(False)
            self.ledger.kill(True, self._stamp())
            self.running = False
            self.generation += 1
            self.stop_event.set()
            self.last_result = {'status': 'blocked', 'reason': 'emergency_stop',
                                'positions_closed': False}
        return self.status()

    def reset_stop(self):
        with self.lock:
            if self.running or (self.thread and self.thread.is_alive()):
                raise ValueError('stop_worker_before_reset')
            self.ledger.kill(False, self._stamp())
        return self.status()

    def _run(self):
        try:
            while not self.stop_event.is_set():
                self.tick()
                delay = min(self.interval * (2 ** min(self.errors, 6)), 300)
                if self.stop_event.wait(delay):
                    break
        finally:
            with self.lock:
                self.running = False

    def _validate(self, snapshot, now):
        if snapshot.get('source') != self.feed.source or snapshot.get('instrument') != self.instrument.as_dict():
            raise ValueError('snapshot_identity_mismatch')
        fresh(snapshot.get('timestamp'), now, 90)
        fresh(snapshot.get('vix_timestamp'), now, 90)
        fresh(timestamp(snapshot.get('bar_timestamp')) + timedelta(minutes=1), now, 180)
        for key in ('price', 'vix', 'atr'):
            number(snapshot.get(key), key, minimum=0.000001)
        self.instrument.validate_trade(now)

    def tick(self):
        if not self.cycle_lock.acquire(blocking=False):
            return {'status': 'busy'}
        try:
            with self.lock:
                if not self.running or self.ledger.killed():
                    return {'status': 'blocked', 'reason': 'stopped'}
                generation = self.generation
            now = timestamp(self.clock())
            is_demo = self.feed.source == 'demo'
            if not is_demo and not self.calendar.is_open(now):
                with self.lock:
                    self.last_risk = {'risk_state': 'closed', 'allow_trade': False,
                                      'reasons': ['Market is closed']}
                    self.last_result = {'status': 'market_closed', 'flatten_pending': bool(
                        self.ledger.account()['positions'])}
                return self.last_result
            snapshot = self.feed.fetch(now)
            self._validate(snapshot, timestamp(self.clock()))
            account = self.ledger.account(mark=(self.instrument.key, snapshot['price']), day=now.date().isoformat())
            snapshot.update(is_market_open=True, drawdown_pct=account['drawdown_pct'])
            risk = self.gate.evaluate(snapshot)
            if account['daily_loss_pct'] >= self.daily_loss_limit:
                risk = {'risk_state': 'high', 'allow_trade': False,
                        'flatten_positions': True, 'reasons': ['Daily paper loss limit reached']}
            closing = False
            if not is_demo:
                closing = now >= self.calendar.bounds(now)[1] - timedelta(minutes=5)
            with self.lock:
                self.last_snapshot = copy.deepcopy(snapshot)
                self.last_risk = risk
            protection_exit = any(
                (p.get('stop_loss') is not None and snapshot['price'] <= p['stop_loss'])
                or (p.get('take_profit') is not None and snapshot['price'] >= p['take_profit'])
                for p in account['positions']
            )
            if protection_exit:
                result = self._commit(snapshot, generation, 'Sell', flatten=True)
                result['exit_reason'] = 'protective_stop_or_target'
            elif not risk['allow_trade'] or closing:
                result = self._commit(snapshot, generation, 'Sell', flatten=True) if (
                    (risk.get('flatten_positions') or closing) and account['positions']
                ) else {'status': 'risk_blocked' if not risk['allow_trade'] else 'closing_window',
                        'reasons': risk['reasons']}
            elif self.auto_enabled:
                result = self._auto_tick(snapshot, account, generation)
            elif self.graph_factory is None:
                result = {'status': 'monitoring', 'reason': 'engine_not_configured'}
            else:
                if self.graph is None:
                    self.graph = self.graph_factory()
                _, signal = self.graph.propagate(self.analysis_symbol or self.instrument.symbol,
                                                now.date().isoformat(), asset_type='stock')
                self.last_decision = str(signal)
                result = self._commit(snapshot, generation, signal)
            with self.lock:
                self.errors = 0
                self.last_error = None
                self.last_result = result
                self.ledger.record(self._stamp(), 'cycle', {
                    'result': result, 'risk': risk, 'decision': self.last_decision,
                    'instrument': self.instrument.key, 'bar': snapshot['bar_timestamp'],
                })
            return result
        except Exception as exc:
            # SDK/provider messages may contain secrets; only expose safe reason codes.
            reason = str(exc) if type(exc) is ValueError and re.fullmatch('[a-z_]{1,80}', str(exc)) else type(exc).__name__
            with self.lock:
                self.errors += 1
                self.last_error = reason
                self.last_risk = {'risk_state': 'unknown', 'allow_trade': False, 'reasons': [reason]}
                self.last_result = {'status': 'error', 'reason': reason}
                self.ledger.record(self._stamp(), 'cycle_error', {'reason': reason, 'count': self.errors})
                if self.errors >= self.max_errors:
                    self.running = False
                    self.stop_event.set()
            return self.last_result
        finally:
            self.cycle_lock.release()

    def start_auto(self, body):
        if not isinstance(body, dict) or set(body) != {'symbol'}:
            raise ValueError('analysis_symbol_required')
        with self.lock:
            if self.feed.source == 'demo':
                raise ValueError('real_market_feed_required_for_auto')
            expected = self.analysis_symbol
            if self.instrument.exchange == 'NSE' and self.instrument.instrument_type == 'EQ':
                expected = self.instrument.symbol.removesuffix('-EQ') + '.NS'
            if not expected or body['symbol'] != expected:
                raise ValueError('analysis_symbol_must_match_trading_instrument')
            if self.graph_factory:
                raise ValueError('cli_engine_active_restart_without_engine_flag')
            self.calendar.bounds(timestamp(self.clock()))
            if self.ledger.killed():
                raise ValueError('emergency_stop_latched')
            self.ai.set_auto(True)
            self.analysis_symbol = expected
            self.auto_enabled = True
            self.last_ai_consumed = None
            self.last_analysis_bar = None
            try:
                return self.start()
            except Exception:
                self.auto_enabled = False
                self.ai.set_auto(False)
                raise

    def _auto_tick(self, snapshot, account, generation):
        ai = self.ai.status()
        result = ai['result']
        if ai['state'] == 'error':
            # One bad authentication/model response must not create an API retry loop.
            self.auto_enabled = False
            self.ai.set_auto(False)
            return {'status': 'blocked', 'reason': 'ai_error_reconnect_required'}
        if result and result['request_id'] != self.last_ai_consumed:
            self.last_ai_consumed = result['request_id']
            context = result.get('intraday_context')
            if not context:
                return {'status': 'blocked', 'reason': 'intraday_context_required'}
            age = (timestamp(self.clock()) - timestamp(context['timestamp'])).total_seconds()
            if age < 0 or age > 300:
                return {'status': 'blocked', 'reason': 'analysis_expired'}
            if abs(snapshot['price'] - context['price']) > snapshot['atr'] * 0.5:
                return {'status': 'blocked', 'reason': 'price_moved_since_analysis'}
            snapshot['decision_bar'] = context['bar_timestamp']
            self.last_decision = result['signal']
            return self._commit(snapshot, generation, result['signal'])
        if ai['worker_busy']:
            return {'status': 'analyzing', 'reason': 'protective_monitoring_continues'}
        if self.last_analysis_bar != snapshot['bar_timestamp']:
            context = {key: snapshot[key] for key in
                       ('price', 'timestamp', 'bar_timestamp', 'vix', 'atr', 'atr_ratio', 'trend_strength')}
            context.update(candles=snapshot.get('chart', [])[-30:],
                           positions=account['positions'], available_paper_cash=account['cash'],
                           session_close=self.calendar.bounds(timestamp(self.clock()))[1].isoformat(),
                           execution_mode='paper', horizon='intraday_only', short_selling=False)
            self.ai.analyze({'symbol': self.analysis_symbol}, intraday_context=context)
            self.last_analysis_bar = snapshot['bar_timestamp']
            return {'status': 'analyzing', 'reason': 'intraday_agents_started'}
        return {'status': 'monitoring', 'reason': 'waiting_for_next_completed_bar'}

    def _commit(self, snapshot, generation, signal, *, flatten=False):
        with self.lock:
            if generation != self.generation or not self.running or self.ledger.killed():
                return {'status': 'blocked', 'reason': 'stopped_during_analysis'}
            now = timestamp(self.clock())
            self._validate(snapshot, now)
            if self.feed.source != 'demo' and not self.calendar.is_open(now):
                return {'status': 'blocked', 'reason': 'market_closed_during_analysis'}
            direction = str(signal).strip().lower()
            if (self.feed.source != 'demo' and direction == 'buy'
                    and now >= self.calendar.bounds(now)[1] - timedelta(minutes=5)):
                return {'status': 'blocked', 'reason': 'closing_window'}
            if direction not in {'buy', 'sell'}:
                return {'status': 'hold', 'reason': 'no_actionable_engine_decision'}
            account = self.ledger.account(mark=(self.instrument.key, snapshot['price']))
            held = sum(p['qty'] for p in account['positions'] if p['instrument'] == self.instrument.key)
            if direction == 'buy' and held:
                return {'status': 'hold', 'reason': 'position_already_open'}
            if direction == 'sell' and not held:
                return {'status': 'hold', 'reason': 'no_position_to_close'}
            price = snapshot['price']
            if direction == 'buy':
                budget = min(account['cash'], self.max_order_value)
                qty = min(math.floor(budget / price), self.max_order_qty,
                          math.floor(account['equity'] * self.risk_fraction / (2 * snapshot['atr'])))
                qty -= qty % self.instrument.lot_size
            else:
                qty = held if flatten else min(held, self.max_order_qty,
                                              math.floor(self.max_order_value / price))
                qty -= qty % self.instrument.lot_size
            if qty <= 0:
                return {'status': 'blocked', 'reason': 'insufficient_budget_for_one_lot'}
            tag = 'flatten' if flatten else 'decision'
            protection = {}
            if direction == 'buy':
                distance = min(2 * snapshot['atr'], price * 0.02)
                protection = {'stop_loss': price - distance, 'take_profit': price + 1.5 * distance}
            decision_bar = snapshot.get('decision_bar', snapshot['bar_timestamp'])
            result = self.ledger.execute(self.instrument, direction, qty, price,
                                         dedupe=f'{tag}:{decision_bar}', stamp=now.isoformat(), **protection)
            self.ledger.account(mark=(self.instrument.key, price))
            return result

    def status(self):
        with self.lock:
            now = timestamp(self.clock())
            try:
                market_open = self.feed.source == 'demo' or self.calendar.is_open(now)
            except ValueError:
                market_open = False
            risk = copy.deepcopy(self.last_risk)
            if self.last_snapshot:
                try:
                    self._validate(self.last_snapshot, now)
                except ValueError:
                    risk = {'risk_state': 'unknown', 'allow_trade': False, 'reasons': ['Market data is stale']}
            killed = self.ledger.killed()
            return {'auto_enabled': self.auto_enabled, 'ai': self.ai.status(), 'mode': 'paper', 'source': self.feed.source,
                    'status': 'running' if self.running else 'stopped',
                    'connection': 'connected' if self.feed.connected else 'not_connected',
                    'instrument': self.instrument.as_dict(), 'analysis_symbol': self.analysis_symbol,
                    'market_open': market_open, 'engine_configured': self.graph_factory is not None,
                    'risk': risk, 'allow_trade': bool(self.running and not killed and market_open
                                                     and risk.get('allow_trade') and (self.graph_factory or self.auto_enabled)),
                    'snapshot': copy.deepcopy(self.last_snapshot),
                    'account': self.ledger.account(), 'last_result': copy.deepcopy(self.last_result),
                    'last_decision': self.last_decision, 'last_error': self.last_error,
                    'error_count': self.errors, 'interval_seconds': self.interval,
                    **self.ledger.history()}

    def close(self):
        self.ai.disconnect()
        self.stop()
        if self.thread:
            self.thread.join(timeout=2)
        # A slow provider may still be unwinding; its daemon cannot commit after stop.
        if not self.thread or not self.thread.is_alive():
            self.ledger.close()
