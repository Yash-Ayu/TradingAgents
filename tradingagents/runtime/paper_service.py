"""One paper-only application owning feed, risk, engine, ledger and scheduler."""
from __future__ import annotations

import copy
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from tradingagents.integrations.angel_one.models import MarketBias
from tradingagents.integrations.angel_one.orchestrator import FOPipelineOrchestrator
from tradingagents.integrations.angel_one.scrip_master import ScripMasterManager

from .ai_analysis import AIAnalysis
from .btst_engine import BTSTStrategyEngine
from .fo_scanner import FOScanner, ScanEvaluationResult, SetupState
from .market_snapshot import IST, SessionCalendar, fresh, number, timestamp
from .safe_runtime import RiskGate

logger = logging.getLogger(__name__)


def _parse_candle_timestamp(ts_val: Any) -> datetime | None:
    """Robustly parse a candle timestamp value into an IST-aware datetime."""
    if isinstance(ts_val, datetime):
        return ts_val if ts_val.tzinfo is not None else ts_val.replace(tzinfo=IST)
    if isinstance(ts_val, str):
        try:
            dt = timestamp(ts_val)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=IST)
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d-%b-%Y %H:%M:%S"):
                try:
                    return datetime.strptime(ts_val, fmt).replace(tzinfo=IST)
                except ValueError:
                    pass
    return None


TRANSIENT_MARKET_DATA_ERRORS = {
    'candle_gap',
    'insufficient_completed_candles',
    'candles_not_strictly_ordered',
    'future_candle',
    'stale_market_data',
    'stale_data',
    'stale_or_future_market_data',
    'quote_fetch_incomplete',
    'angel_rate_limited',
    'angel_timeout',
    'angel_connection_error',
    'angel_request_failed',
    'angel_data_missing',
    'dhan_rate_limited',
    'dhan_timeout',
    'dhan_auth_failed',
    'dhan_connection_error',
    'invalid_quote_timestamp',
    'quote_timestamp_missing',
    'RateLimitExceeded',
    'TimeoutError',
    'ConnectionError',
}


def is_transient_market_error(reason: str, exc: Exception | None = None) -> bool:
    """Classify broker-feed and market data gaps that should backoff rather than kill the runner."""
    if reason in TRANSIENT_MARKET_DATA_ERRORS:
        return True
    lower = reason.lower()
    if any(k in lower for k in ('candle', 'stale', 'rate_limit', 'timeout', 'connection', 'network')):
        return True
    if exc is not None:
        exc_name = type(exc).__name__
        if exc_name in ('TimeoutError', 'ConnectionError', 'RateLimitExceeded'):
            return True
        if any(k in exc_name.lower() for k in ('timeout', 'connection', 'ratelimit', 'network')):
            return True
    return False


class PaperTradingService:
    def __init__(self, feed, ledger, *, graph_factory=None, analysis_symbol=None,
                 calendar=None, interval=60, max_errors=3, max_order_value=10000,
                 max_order_qty=50, risk_fraction=0.01, daily_loss_limit=3.0, clock=None,
                 scrip_master=None, scanner_batch_size=12):
        self.feed, self.ledger = feed, ledger
        self.instrument = feed.instrument
        self.graph_factory = graph_factory
        self.graph = None
        self.ai = AIAnalysis()
        if self.graph_factory is not None:
            self.ai.graph_builder = lambda config, key: self.graph_factory()
            self.ai.state = 'ready'
            self.ai.config = {'engine': 'cli_engine', 'llm_provider': 'cli_engine', 'quick_think_llm': 'cli_engine'}
            self.ai.progress = 'CLI Engine active. Stock select karke Analyze dabayein.'
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
        if clock is not None and hasattr(self.feed, 'clock') and not getattr(self.feed, '_custom_clock', False):
            self.feed.clock = self.clock
            self.feed._custom_clock = True
        self.gate = RiskGate()
        self.lock = threading.RLock()
        self.cycle_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
        self.generation = 0
        self.errors = 0
        self.non_transient_errors = 0
        self.last_error = None
        self.last_snapshot = None
        self.last_result = {'status': 'stopped'}
        self.last_decision = None
        self.last_risk = {'risk_state': 'unknown', 'allow_trade': False, 'reasons': ['No market data']}
        self.ledger.bind(f'{self.feed.source}:{self.instrument.key}')
        self.scrip_master = scrip_master or ScripMasterManager()
        self.fo_orchestrator = FOPipelineOrchestrator(scrip_master=self.scrip_master)
        self.fo_scanner = FOScanner(scrip_master=self.scrip_master)
        self.btst_engine = BTSTStrategyEngine()
        self.scanner_batch_size = max(1, int(scanner_batch_size))
        self.scanner_cadence_seconds = 300.0
        self._last_scan_mono = 0.0
        self._scanner_rotation_idx = 0
        self.last_scanner_result: dict = {
            'universe_count': 0,
            'screened_count': 0,
            'scanned_count': 0,
            'shortlisted_count': 0,
            'scanner_evaluated_count': 0,
            'executable_count': 0,
            'last_execution_rejection': None,
            'shortlist_telemetry': [],
            'active_candidates': [],
            'rejected_count': 0,
            'data_unavailable_count': 0,
            'scan_duration': 0.0,
            'last_scan_timestamp': None,
            'batch_size': self.scanner_batch_size,
            'rotation_index': 0,
            'next_rotation_index': 0,
            'rejected_by_reason': {},
            'data_source': self.feed.source,
            'evaluations': [],
            'execution_allowed': False,
            'index_execution_allowed': False,
        }
        self.last_btst_result: dict = {
            'window_active': False,
            'window_status': 'Initial idle state',
            'timestamp': None,
            'candidates': [],
        }

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
            self.non_transient_errors = 0
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
            if self.thread and self.thread.is_alive() and self.stop_event.is_set():
                self.thread.join(timeout=0.5)
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
        if hasattr(self.instrument, 'validate_reference'):
            self.instrument.validate_reference(now)
        elif getattr(self.instrument, 'instrument_type', None) != 'INDEX':
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
            elif closing:
                if account['positions']:
                    result = self._commit(snapshot, generation, 'Sell', flatten=True)
                else:
                    result = {'status': 'closing_window', 'reasons': ['Market is closing']}
            elif (risk.get('flatten_positions') and account['positions']):
                result = self._commit(snapshot, generation, 'Sell', flatten=True)
            elif not risk['allow_trade']:
                # Broad index reference risk is blocked (e.g. NIFTY weak / choppy / high VIX).
                # Index derivatives cannot execute, but strong individual stock F&O and BTST setups CAN execute!
                hard_stop = (
                    account['daily_loss_pct'] >= self.daily_loss_limit
                    or self.ledger.killed()
                    or getattr(self.fo_orchestrator.risk_engine, 'is_kill_switched', False)
                )
                fo_result = self._scan_and_execute_fo(
                    snapshot, account, generation,
                    allow_execution=not hard_stop,
                    allow_index_execution=False
                )
                result = fo_result or {'status': 'risk_blocked', 'reasons': risk['reasons']}
            elif self.auto_enabled:
                result = self._auto_tick(snapshot, account, generation)
            elif self.graph_factory is None:
                result = {'status': 'monitoring', 'reason': 'engine_not_configured'}
            else:
                fo_result = self._scan_and_execute_fo(snapshot, account, generation, allow_execution=True, allow_index_execution=True)
                if fo_result:
                    result = fo_result
                else:
                    if self.graph is None:
                        self.graph = self.graph_factory()
                    _, signal = self.graph.propagate(self.analysis_symbol or self.instrument.symbol,
                                                    now.date().isoformat(), asset_type='stock')
                    self.last_decision = str(signal)
                    result = self._commit(snapshot, generation, signal)

            if not self.fo_orchestrator.paper_engine.get_open_positions():
                self.fo_orchestrator.paper_engine.mark_positions_unknown_data(False)
            with self.lock:
                self.errors = 0
                self.non_transient_errors = 0
                self.last_error = None
                self.last_result = result
                self.ledger.record(self._stamp(), 'cycle', {
                    'result': result, 'risk': risk, 'decision': self.last_decision,
                    'instrument': self.instrument.key, 'bar': snapshot['bar_timestamp'],
                })
            return result
        except Exception as exc:
            # Log exception location only, never SDK/provider messages or secrets.
            tb = exc.__traceback__
            while tb and tb.tb_next:
                tb = tb.tb_next
            if tb:
                logger.warning(
                    "Cycle diagnostic: exception_type=%s file=%s line=%d",
                    type(exc).__name__,
                    tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1],
                    tb.tb_lineno,
                )
            # SDK/provider messages may contain secrets; only expose safe reason codes.
            reason = str(exc) if type(exc) is ValueError and re.fullmatch('[a-z_]{1,80}', str(exc)) else type(exc).__name__
            is_transient = is_transient_market_error(reason, exc)
            self.fo_orchestrator.paper_engine.mark_positions_unknown_data(True)
            with self.lock:
                self.errors += 1
                self.last_error = reason
                self.last_risk = {'risk_state': 'unknown', 'allow_trade': False, 'reasons': [reason]}
                self.last_result = {'status': 'error', 'reason': reason}
                self.ledger.record(self._stamp(), 'cycle_error', {
                    'reason': reason, 'count': self.errors, 'transient': is_transient
                })
                if not is_transient:
                    self.non_transient_errors += 1
                    if self.non_transient_errors >= self.max_errors:
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
            elif not expected and self.instrument.instrument_type == 'INDEX':
                expected = self.instrument.symbol
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

    def _sync_fo_positions(self, snapshot):
        for pos in self.fo_orchestrator.paper_engine.get_open_positions():
            sym = pos.symbol
            ltp = None
            is_real_quote = False

            # 1. Check live WebSocket market data provider tick
            if self.fo_orchestrator.market_data_provider is not None:
                token = getattr(pos.contract, 'symbol_token', None)
                if token:
                    tick = self.fo_orchestrator.market_data_provider.get_latest_tick(token)
                    if tick and not tick.is_stale and tick.ltp > 0:
                        ltp = tick.ltp
                        is_real_quote = True

            # 2. Try market adapter quote snapshot for option symbol
            if ltp is None:
                try:
                    from .market_adapter import get_active_market_adapter
                    adapter = get_active_market_adapter()
                    if adapter and hasattr(adapter, 'get_quote'):
                        quote = adapter.get_quote(sym)
                        if quote and quote.get('ltp'):
                            candidate_ltp = float(quote['ltp'])
                            if candidate_ltp > 0 and not quote.get('is_stale', False):
                                ltp = candidate_ltp
                                is_real_quote = True
                except Exception:
                    pass

            if is_real_quote and ltp is not None and ltp > 0:
                # Real option tick/quote data received: resume exits and evaluate SL/Target triggers
                self.fo_orchestrator.paper_engine.mark_positions_unknown_data(False, symbol=sym)
                self.fo_orchestrator.paper_engine.update_mark_price(sym, ltp)
            else:
                # Real option quote unavailable or stale:
                # Do NOT use synthetic intrinsic/time-decay estimate to trigger SL/Target!
                # Mark position data as unknown/stale and fail closed until reliable option price returns.
                self.fo_orchestrator.paper_engine.mark_positions_unknown_data(True, symbol=sym)

    def _validate_symbol_candles(self, sym: str, df: pd.DataFrame, source: str, now: datetime, is_index: bool = False) -> tuple[bool, str]:
        """Strict data quality, freshness, and sanity checks for per-symbol candles."""
        if df is None or len(df) < 25:
            return False, f"insufficient_bars_{len(df) if df is not None else 0}"

        required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in required_cols:
            if col not in df.columns:
                return False, f"missing_column_{col}"

        # 1. Finite numbers and positivity
        for col in required_cols:
            vals = df[col].values
            if not np.all(np.isfinite(vals)):
                return False, f"non_finite_values_in_{col}"

        prices = df['Close'].values
        if np.any(prices <= 0.0):
            return False, "non_positive_price_detected"

        # 2. Strict OHLC hierarchy check: High >= Low, High >= Open, High >= Close, Low <= Open, Low <= Close
        if np.any(df['High'] < df['Low']) or np.any(df['High'] < df['Open']) or np.any(df['High'] < df['Close']):
            return False, "malformed_ohlc_relationship"
        if np.any(df['Low'] > df['Open']) or np.any(df['Low'] > df['Close']):
            return False, "malformed_ohlc_relationship"

        # 3. Flat / dead feed detection: price std and range over last 20 bars
        recent_closes = prices[-20:]
        if float(np.std(recent_closes)) <= 1e-6:
            return False, "flat_dead_feed_zero_std"

        recent_range = float(df['High'].iloc[-20:].max() - df['Low'].iloc[-20:].min())
        if recent_range <= 1e-6:
            return False, "flat_dead_feed_zero_range"

        # Volume validation:
        # For stocks: zero/flat volume remains a hard data-quality failure.
        # For cash indices (NIFTY, BANKNIFTY, etc.): volume is structurally unavailable (reported as 0),
        # so validate OHLC movement, range, std, and freshness instead of rejecting as dead feed.
        if not is_index and float(df['Volume'].iloc[-20:].sum()) <= 0:
            return False, "dead_feed_zero_volume"

        # 4. Timestamp & Freshness validation
        if 'Timestamp' in df.columns:
            last_ts = df['Timestamp'].iloc[-1]
            last_dt = _parse_candle_timestamp(last_ts)
            if last_dt is None:
                return False, "invalid_or_missing_candle_timestamp"
            if last_dt > now + timedelta(minutes=5):
                return False, "future_candle_timestamp"
            # If live Angel market is open, verify freshness (maximum 15 min / 3 bars staleness)
            if self.feed.source == 'angel' and self.calendar.is_open(now):
                age_seconds = (now - last_dt).total_seconds()
                if age_seconds > 900:
                    return False, f"stale_candle_data_age_{int(age_seconds)}s"

        return True, "valid"

    def _scan_and_execute_fo(self, snapshot, account, generation, allow_execution=True, allow_index_execution=True, force=False):
        start_time = time.monotonic()
        now = timestamp(self.clock())

        # Always sync existing open F&O positions before scanning new setups
        self._sync_fo_positions(snapshot)

        now_mono = time.monotonic()
        if not force and self._last_scan_mono > 0.0 and (now_mono - self._last_scan_mono) < self.scanner_cadence_seconds:
            logger.debug(f"Scanner cadence active ({(now_mono - self._last_scan_mono):.1f}s < {self.scanner_cadence_seconds}s). Skipping scan.")
            return None

        self._last_scan_mono = now_mono

        # Ensure orchestrator and scanner share authoritative scrip master
        self.fo_scanner.scrip_master = self.scrip_master
        self.fo_orchestrator.scrip_master = self.scrip_master
        self.fo_orchestrator.resolver.scrip_master = self.scrip_master

        # Stage 1: Dynamic Universe Discovery from Scrip Master
        fo_universe = self.scrip_master.get_fo_universe()
        universe_count = len(fo_universe)
        if universe_count == 0:
            self.last_scanner_result = {
                'universe_count': 0,
                'screened_count': 0,
                'scanned_count': 0,
                'shortlisted_count': 0,
                'scanner_evaluated_count': 0,
                'active_candidates': [],
                'rejected_count': 0,
                'data_unavailable_count': 0,
                'scan_duration': 0.0,
                'last_scan_timestamp': now.isoformat(),
                'batch_size': self.scanner_batch_size,
                'rotation_index': self._scanner_rotation_idx,
                'next_rotation_index': self._scanner_rotation_idx,
                'rejected_by_reason': {'empty_universe': 1},
                'data_source': self.feed.source,
                'evaluations': [],
                'execution_allowed': bool(allow_execution),
                'index_execution_allowed': bool(allow_index_execution),
            }
            return None

        # Prioritized universe: indices -> liquid stocks -> remaining F&O underlyings
        ordered_symbols = self.fo_scanner.discover_universe()

        # If analysis_symbol is configured, give it priority in screening
        if self.analysis_symbol:
            clean_analysis = self.analysis_symbol.removesuffix('.NS').removesuffix('-EQ')
            if clean_analysis in fo_universe and clean_analysis in ordered_symbols:
                ordered_symbols.remove(clean_analysis)
                ordered_symbols.insert(0, clean_analysis)

        # Stage 2: Workload Bounding and Rotation to respect API limits
        batch_size = self.scanner_batch_size
        indices = [s for s in ordered_symbols if fo_universe.get(s, {}).get('is_index', False)]

        if len(ordered_symbols) <= batch_size:
            symbols_to_screen = list(ordered_symbols)
            start_idx = 0
            self._scanner_rotation_idx = 0
        else:
            # Allocate up to 3 slots for benchmark indices, remaining slots for rotating stocks
            priority_indices = indices[:min(3, len(indices))]
            remaining_slots = max(1, batch_size - len(priority_indices))
            pool = [s for s in ordered_symbols if s not in priority_indices]
            if pool:
                start_idx = self._scanner_rotation_idx % len(pool)
                end_idx = start_idx + remaining_slots
                if end_idx <= len(pool):
                    rotating_slice = pool[start_idx:end_idx]
                else:
                    rotating_slice = pool[start_idx:] + pool[:end_idx - len(pool)]
                self._scanner_rotation_idx = (start_idx + remaining_slots) % len(pool)
            else:
                rotating_slice = []
                start_idx = 0
            symbols_to_screen = priority_indices + rotating_slice

        # BTST Window check
        in_btst, window_status = self.btst_engine.is_in_btst_window(now)
        btst_candidates = []

        curr_sym = self.instrument.symbol.removesuffix('-EQ').removesuffix('.NS')
        screened_count = 0
        shortlisted_count = 0
        scanner_evaluated_count = 0
        rejected_count = 0
        data_unavailable_count = 0
        scanned_candidates = []
        evaluations = []
        rejected_by_reason: dict[str, int] = {}
        executed_result = None

        # Stage 2b: Data Acquisition & Fast Lightweight Screening
        shortlisted_items = []

        for sym in symbols_to_screen:
            screened_count += 1
            df = None
            candle_source = self.feed.source
            is_index = fo_universe.get(sym, {}).get('is_index', False) or sym in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "Nifty 50", "Nifty Bank"}

            # Guardrail 4: Per-symbol real market data isolation.
            # Never reuse currently selected instrument's snapshot chart for any other symbol.
            if sym == curr_sym and snapshot and len(snapshot.get('chart', [])) >= 25:
                chart_data = snapshot['chart']
                try:
                    df = pd.DataFrame(chart_data, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                    candle_source = 'SNAPSHOT_PRIMARY'
                except Exception:
                    df = None
            else:
                try:
                    from .market_adapter import get_active_market_adapter
                    adapter = get_active_market_adapter(preferred_source=self.feed.source)
                    candles = adapter.get_candles(sym, interval='5m')
                    candle_source = candles.get('source', 'UNKNOWN')

                    # Guardrail 5: Provenance gate — Reject fallback/synthetic data masquerading as Angel live data
                    if self.feed.source == 'angel' and candle_source != 'LIVE_ANGEL_ONE':
                        fail_reason = f"provenance_rejected_{candle_source}"
                        data_unavailable_count += 1
                        rejected_count += 1
                        rejected_by_reason[fail_reason] = rejected_by_reason.get(fail_reason, 0) + 1
                        evaluations.append(ScanEvaluationResult(
                            symbol=sym, underlying=sym, spot_price=0.0,
                            state=SetupState.NO_SETUP, bias=MarketBias.NEUTRAL,
                            reason=f"Data rejected: {fail_reason}. Fail-closed."
                        ))
                        continue

                    # Guardrail 3: Schema extraction from candles['chart']
                    raw_chart = candles.get('chart') or candles.get('rows', [])
                    if isinstance(raw_chart, list) and len(raw_chart) >= 25:
                        if all(isinstance(r, (list, tuple)) and len(r) >= 6 for r in raw_chart[:25]):
                            df = pd.DataFrame([r[:6] for r in raw_chart], columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                        else:
                            df = None
                except Exception as exc:
                    if str(exc) == "angel_rate_limited":
                        logger.warning(
                            "Angel rate limit encountered while scanning %s; "
                            "stopping current scanner batch to avoid further API requests.",
                            sym,
                        )
                        break
                    logger.debug(f"Could not retrieve candles for {sym}: {exc}")
                    df = None
                    exc_str = str(exc).lower()
                    if "rate_limit" in exc_str or "ab1004" in exc_str or "429" in exc_str:
                        logger.warning(f"Angel One rate limit encountered during screening on {sym}. Halting scanner batch immediately.")
                        rejected_by_reason["RATE_LIMITED"] = rejected_by_reason.get("RATE_LIMITED", 0) + 1
                        break

            # Guardrail 6 & 7: Stale, flat, dead, or missing data validation
            if df is None:
                fail_reason = "missing_or_malformed_candles"
                data_unavailable_count += 1
                rejected_count += 1
                rejected_by_reason[fail_reason] = rejected_by_reason.get(fail_reason, 0) + 1
                evaluations.append(ScanEvaluationResult(
                    symbol=sym, underlying=sym, spot_price=0.0,
                    state=SetupState.NO_SETUP, bias=MarketBias.NEUTRAL,
                    reason="Market data unavailable or malformed. Fail-closed."
                ))
                continue

            valid, reason = self._validate_symbol_candles(sym, df, candle_source, now, is_index=is_index)
            if not valid:
                if "stale" in reason or "missing" in reason or "insufficient" in reason or "timestamp" in reason:
                    data_unavailable_count += 1
                rejected_count += 1
                rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1
                evaluations.append(ScanEvaluationResult(
                    symbol=sym, underlying=sym, spot_price=float(df['Close'].iloc[-1]) if 'Close' in df.columns and len(df) > 0 else 0.0,
                    state=SetupState.NO_SETUP, bias=MarketBias.NEUTRAL,
                    reason=f"Data quality gate failed: {reason}. Fail-closed."
                ))
                continue

            # Stage 3: Candidate qualified for deep technical analysis
            latest_price = float(df['Close'].iloc[-1])
            shortlisted_count += 1
            shortlisted_items.append((sym, df, latest_price))

        # Stage 4: Deeper F&O Scanning / Early-Setup Evaluation / Orchestration
        shortlist_telemetry = []
        executable_count = 0
        last_execution_rejection = None

        for sym, df, _spot_price in shortlisted_items:
            scanner_evaluated_count += 1
            is_index = fo_universe.get(sym, {}).get('is_index', False)
            can_execute = allow_execution and (allow_index_execution or not is_index)
            cand_telemetry = {
                'symbol': sym,
                'is_index': is_index,
                'state': 'UNKNOWN',
                'setup_name': None,
                'rejection_reason': None,
                'execution_status': 'NOT_EXECUTED',
            }

            # 4a. BTST Evaluation if in window
            if in_btst:
                try:
                    btst_res = self.btst_engine.evaluate_btst_candidate(sym, df, eval_time=now)
                    if btst_res.candidate is not None:
                        btst_candidates.append(btst_res.candidate)
                        cand_telemetry['setup_name'] = btst_res.candidate.strategy_id
                        if can_execute and not executed_result and len(self.fo_orchestrator.paper_engine.get_open_positions()) < self.fo_orchestrator.risk_engine.config.max_open_positions:
                            contract_budget = min(account['cash'], self.max_order_value)
                            est_prem = max(10.0, btst_res.candidate.spot_price * 0.015)
                            lot_size = fo_universe.get(sym, {}).get('lot_size', 50)
                            lots = max(1, min(self.max_order_qty, math.floor(contract_budget / max(1.0, est_prem * lot_size))))
                            pipe_res = self.fo_orchestrator.process_signal(
                                underlying=sym,
                                spot_price=btst_res.candidate.spot_price,
                                bias=btst_res.candidate.bias,
                                proposed_lots=lots,
                                stop_loss=btst_res.candidate.stop_loss,
                                target=btst_res.candidate.target,
                                evaluation_time=now,
                                strategy_name=btst_res.candidate.strategy_id,
                                trade_type="BTST",
                                data_source="LIVE_ANGEL_ONE" if self.feed.source == 'angel' else "SIMULATION",
                            )
                            if pipe_res.success:
                                executed_result = {
                                    'status': 'executed_btst_paper',
                                    'trade_type': 'BTST',
                                    'contract': pipe_res.contract.trading_symbol if pipe_res.contract else sym,
                                    'lots': lots
                                }
                                executable_count += 1
                                cand_telemetry['execution_status'] = 'EXECUTED_BTST'
                            else:
                                last_execution_rejection = pipe_res.reason or pipe_res.status
                                cand_telemetry['rejection_reason'] = last_execution_rejection
                        elif not can_execute:
                            cand_telemetry['rejection_reason'] = 'RISK_BLOCKED'
                            last_execution_rejection = 'RISK_BLOCKED'
                        elif len(self.fo_orchestrator.paper_engine.get_open_positions()) >= self.fo_orchestrator.risk_engine.config.max_open_positions:
                            cand_telemetry['rejection_reason'] = 'MAX_OPEN_POSITIONS'
                            last_execution_rejection = 'MAX_OPEN_POSITIONS'
                except Exception as exc:
                    logger.warning(f"BTST evaluation failed for {sym}: {exc}")

            # 4b. Intraday Early Setup F&O Evaluation
            try:
                res = self.fo_scanner.evaluate_price_action(sym, df, is_index=is_index)
                evaluations.append(res)
                cand_telemetry['state'] = res.state.value if hasattr(res.state, 'value') else str(res.state)
                if res.candidate is not None:
                    scanned_candidates.append(res.candidate)
                    cand_telemetry['setup_name'] = res.candidate.setup_name
                    if can_execute and not executed_result and len(self.fo_orchestrator.paper_engine.get_open_positions()) < self.fo_orchestrator.risk_engine.config.max_open_positions:
                        contract_budget = min(account['cash'], self.max_order_value)
                        est_prem = max(10.0, res.candidate.spot_price * 0.015)
                        lot_size = fo_universe.get(sym, {}).get('lot_size', 50)
                        lots = max(1, min(self.max_order_qty, math.floor(contract_budget / max(1.0, est_prem * lot_size))))
                        pipe_res = self.fo_orchestrator.process_signal(
                            underlying=sym,
                            spot_price=res.candidate.spot_price,
                            bias=res.candidate.bias,
                            proposed_lots=lots,
                            stop_loss=res.candidate.stop_loss,
                            target=res.candidate.target,
                            evaluation_time=now,
                            strategy_name=res.candidate.setup_name,
                            trade_type="INTRADAY",
                            data_source="LIVE_ANGEL_ONE" if self.feed.source == 'angel' else "SIMULATION",
                        )
                        if pipe_res.success:
                            executed_result = {
                                'status': 'executed_intraday_fo_paper',
                                'trade_type': 'INTRADAY',
                                'contract': pipe_res.contract.trading_symbol if pipe_res.contract else sym,
                                'lots': lots
                            }
                            executable_count += 1
                            cand_telemetry['execution_status'] = 'EXECUTED_INTRADAY'
                        else:
                            last_execution_rejection = pipe_res.reason or pipe_res.status
                            cand_telemetry['rejection_reason'] = last_execution_rejection
                    elif not can_execute:
                        rej_code = 'INDEX_EXECUTION_BLOCKED' if is_index else 'RISK_BLOCKED'
                        cand_telemetry['rejection_reason'] = rej_code
                        last_execution_rejection = rej_code
                    elif len(self.fo_orchestrator.paper_engine.get_open_positions()) >= self.fo_orchestrator.risk_engine.config.max_open_positions:
                        cand_telemetry['rejection_reason'] = 'MAX_OPEN_POSITIONS'
                        last_execution_rejection = 'MAX_OPEN_POSITIONS'
                    elif executed_result is not None:
                        cand_telemetry['rejection_reason'] = 'MAX_ONE_TRADE_PER_TICK'
                else:
                    rejected_count += 1
                    rej_code = res.state.value if hasattr(res.state, 'value') else str(res.state)
                    rejected_by_reason[rej_code] = rejected_by_reason.get(rej_code, 0) + 1
                    cand_telemetry['rejection_reason'] = rej_code
            except Exception as exc:
                logger.warning(f"FO Scanner evaluation failed for {sym}: {exc}")
                rejected_count += 1
                rejected_by_reason['evaluation_exception'] = rejected_by_reason.get('evaluation_exception', 0) + 1
                cand_telemetry['rejection_reason'] = 'EVALUATION_EXCEPTION'

            shortlist_telemetry.append(cand_telemetry)

        # Finalize and Populate Telemetry BEFORE returning execution result
        scan_duration = round(time.monotonic() - start_time, 4)
        self.last_btst_result = {
            'window_active': in_btst,
            'window_status': window_status,
            'timestamp': now.isoformat(),
            'candidates': [c.__dict__ if hasattr(c, '__dict__') else c for c in btst_candidates],
        }
        self.last_scanner_result = {
            'universe_count': universe_count,
            'screened_count': screened_count,
            'scanned_count': screened_count,
            'shortlisted_count': shortlisted_count,
            'scanner_evaluated_count': scanner_evaluated_count,
            'executable_count': executable_count,
            'last_execution_rejection': last_execution_rejection,
            'shortlist_telemetry': shortlist_telemetry,
            'active_candidates': [c.__dict__ if hasattr(c, '__dict__') else c for c in scanned_candidates],
            'rejected_count': rejected_count,
            'data_unavailable_count': data_unavailable_count,
            'scan_duration': scan_duration,
            'last_scan_timestamp': now.isoformat(),
            'batch_size': batch_size,
            'rotation_index': start_idx,
            'next_rotation_index': self._scanner_rotation_idx,
            'rejected_by_reason': rejected_by_reason,
            'data_source': self.feed.source,
            'evaluations': [{'symbol': e.symbol, 'state': e.state.value if hasattr(e.state, 'value') else str(e.state), 'reason': e.reason} for e in evaluations],
            'execution_allowed': bool(allow_execution),
            'index_execution_allowed': bool(allow_index_execution),
        }

        if executed_result:
            return executed_result
        return None

    def _auto_tick(self, snapshot, account, generation):
        self._sync_fo_positions(snapshot)
        fo_exec = self._scan_and_execute_fo(snapshot, account, generation, allow_execution=True)
        if fo_exec:
            return fo_exec

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
            is_valid_research_ticker = (
                bool(self.analysis_symbol)
                and isinstance(self.analysis_symbol, str)
                and bool(re.fullmatch(r'[A-Za-z0-9^][A-Za-z0-9.^=-]{0,39}', self.analysis_symbol))
                and self.analysis_symbol.upper() not in {'DEMO', 'DEMO-EQ'}
            )
            if not is_valid_research_ticker:
                return {'status': 'monitoring', 'reason': 'waiting_for_next_completed_bar'}
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
            # Strict trade execution validation: an INDEX is reference-only and cannot be executed directly
            try:
                self.instrument.validate_trade(now)
            except ValueError as exc:
                reason = 'non_tradable_execution_instrument' if str(exc) == 'non_tradable_instrument' else str(exc)
                return {'status': 'blocked', 'reason': reason}
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
            is_stale = False
            if self.last_snapshot:
                try:
                    self._validate(self.last_snapshot, now)
                except ValueError:
                    is_stale = True
                    # If risk state was allowed or unspecified, reflect stale data condition
                    if risk.get('risk_state') in ('normal', 'low', 'unknown'):
                        risk['allow_trade'] = False
                        if 'Market data is stale' not in risk.get('reasons', []):
                            risk['reasons'] = ['Market data is stale'] + [r for r in risk.get('reasons', []) if r != 'Market data is stale']

            market_data_info = {
                'primary': self.feed.source,
                'active': self.feed.source,
                'fallback_active': False,
                'reason': None,
                'health': {self.feed.source: 'healthy' if self.feed.connected else 'disconnected'},
                'last_candle_timestamp': None,
                'last_quote_timestamp': None,
            }
            try:
                from .market_adapter import get_active_market_adapter
                adapter = get_active_market_adapter(preferred_source=self.feed.source)
                if hasattr(adapter, 'get_status'):
                    market_data_info.update(adapter.get_status())
                elif hasattr(adapter, 'name'):
                    market_data_info['active'] = adapter.name
            except Exception:
                pass

            killed = self.ledger.killed()
            fo_pos = [p.model_dump() for p in self.fo_orchestrator.paper_engine.get_open_positions()]
            fo_orders = self.fo_orchestrator.paper_engine.get_orders(20)
            fo_history = self.fo_orchestrator.paper_engine.get_trade_history(20)
            fo_metrics = self.fo_orchestrator.paper_engine.get_performance_metrics().model_dump()
            return {'auto_enabled': self.auto_enabled, 'ai': self.ai.status(), 'mode': 'paper', 'live_execution': False, 'source': self.feed.source,
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
                    'fo_positions': fo_pos, 'fo_orders': fo_orders, 'fo_history': fo_history,
                    'fo_metrics': fo_metrics, 'fo_scanner': copy.deepcopy(self.last_scanner_result),
                    'btst': copy.deepcopy(self.last_btst_result),
                    'market_data': market_data_info,
                    'is_data_stale': is_stale or bool(risk.get('reasons') and any('stale' in r.lower() for r in risk.get('reasons', []))),
                    'fallback_active': market_data_info.get('fallback_active', False),
                    **self.ledger.history()}

    def close(self):
        self.ai.disconnect()
        self.stop()
        if self.thread:
            self.thread.join(timeout=2)
        # A slow provider may still be unwinding; its daemon cannot commit after stop.
        if not self.thread or not self.thread.is_alive():
            self.ledger.close()

