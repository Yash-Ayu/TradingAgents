"""
Deterministic Risk Engine for Indian F&O Trading.
Enforces hard mathematical, timing, capital, and position guardrails before execution.
AI/LLM has ZERO authority to alter, override, or bypass these rules.
"""

import logging
from datetime import date, datetime, time
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .models import ContractSpec

logger = logging.getLogger(__name__)


class RiskConfig(BaseModel):
    """Configurable risk parameters for deterministic risk engine."""
    max_capital_per_trade: float = Field(default=50000.0, gt=0, description="Max capital in INR per trade")
    max_lots_per_trade: int = Field(default=2, gt=0, description="Max lots allowed per single trade")
    max_open_positions: int = Field(default=2, gt=0, description="Max concurrent open positions allowed")
    max_daily_trades: int = Field(default=10, gt=0, description="Max trades allowed in one trading session")
    max_daily_loss: float = Field(default=10000.0, gt=0, description="Cumulative daily loss limit before auto-lock")
    max_loss_per_trade: float = Field(default=3000.0, gt=0, description="Max acceptable loss per trade based on SL")
    market_open_time: time = Field(default=time(9, 15), description="Market start time (IST)")
    market_close_time: time = Field(default=time(15, 30), description="Market close time (IST)")
    cutoff_time: time = Field(default=time(15, 15), description="No new entries allowed after this time (IST)")
    max_data_age_seconds: float = Field(default=10.0, gt=0, description="Max age of market quote in seconds")
    max_bid_ask_spread_pct: float = Field(default=3.0, gt=0, description="Max allowed bid-ask spread in %")
    enforce_market_hours: bool = Field(default=True, description="Enforce strict IST market hours checks")
    enforce_stop_loss: bool = Field(default=True, description="Strictly require valid stop loss for every trade")


class RiskEvaluationRequest(BaseModel):
    """Input payload for deterministic pre-trade evaluation."""
    contract: ContractSpec
    action: str = Field(pattern="^(BUY|SELL)$", description="Trade action: BUY or SELL")
    proposed_lots: int = Field(gt=0, description="Proposed lot count")
    entry_price: float = Field(gt=0, description="Estimated/current entry price")
    stop_loss: Optional[float] = Field(default=None, description="Mandatory stop loss price")
    target: Optional[float] = Field(default=None, description="Optional target price")
    quote_timestamp: Optional[datetime] = Field(default=None, description="Timestamp of the market price quote")
    bid_price: Optional[float] = Field(default=None, description="Current market bid price")
    ask_price: Optional[float] = Field(default=None, description="Current market ask price")
    evaluation_time: Optional[datetime] = Field(default=None, description="Override time for testing/simulation")


class RiskEvaluationResult(BaseModel):
    """Deterministic risk evaluation output."""
    approved: bool
    reason: str
    allocated_lots: int = 0
    allocated_capital: float = 0.0
    max_loss_risk: float = 0.0
    violation_code: Optional[str] = None
    evaluated_at: datetime = Field(default_factory=datetime.now)


class DeterministicRiskEngine:
    """
    Pure Python rule-based risk enforcement engine.
    Guarantees FAIL-CLOSED semantics on any violation or uncertainty.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self.daily_trades_count = 0
        self.daily_realized_pnl = 0.0
        self.current_open_positions: Dict[str, Dict[str, Any]] = {}
        self.is_emergency_locked = False
        self.lock_reason: Optional[str] = None
        self.session_date: date = date.today()

    def _check_and_reset_daily_state(self, current_dt: datetime) -> None:
        """Automatically reset daily counters on a new trading session date."""
        if current_dt.date() > self.session_date:
            logger.info(f"New session detected ({current_dt.date()}). Resetting daily risk metrics.")
            self.session_date = current_dt.date()
            self.daily_trades_count = 0
            self.daily_realized_pnl = 0.0
            # Note: Open positions persist until explicitly closed

    def trigger_kill_switch(self, reason: str) -> None:
        """Activate emergency lock. Blocks ALL future trade evaluations."""
        self.is_emergency_locked = True
        self.lock_reason = reason
        logger.critical(f"EMERGENCY KILL SWITCH ACTIVATED: {reason}")

    def unlock_kill_switch(self, reason: str) -> None:
        """Explicit administrative release of emergency lock."""
        self.is_emergency_locked = False
        self.lock_reason = None
        logger.warning(f"Emergency kill switch released. Reason: {reason}")

    def evaluate(self, request: RiskEvaluationRequest) -> RiskEvaluationResult:
        """
        Evaluate a trade proposal against all configured deterministic rules.
        Returns approved=True ONLY if ALL checks pass.
        """
        eval_dt = request.evaluation_time or datetime.now()
        self._check_and_reset_daily_state(eval_dt)

        # 1. EMERGENCY KILL SWITCH CHECK
        if self.is_emergency_locked:
            return RiskEvaluationResult(
                approved=False,
                reason=f"EMERGENCY KILL SWITCH ACTIVE: {self.lock_reason}. No trades permitted.",
                violation_code="KILL_SWITCH_ACTIVE",
            )

        # 2. DAILY LOSS LIMIT CHECK (Circuit Breaker)
        if self.daily_realized_pnl <= -self.config.max_daily_loss:
            self.trigger_kill_switch(
                f"Max daily loss reached ({self.daily_realized_pnl:.2f} <= -{self.config.max_daily_loss:.2f})"
            )
            return RiskEvaluationResult(
                approved=False,
                reason=f"Cumulative daily loss (-₹{abs(self.daily_realized_pnl):.2f}) reached limit. Auto-lock triggered.",
                violation_code="MAX_DAILY_LOSS_EXCEEDED",
            )

        # 3. DAILY TRADES COUNT CHECK
        if self.daily_trades_count >= self.config.max_daily_trades:
            return RiskEvaluationResult(
                approved=False,
                reason=f"Max daily trades limit ({self.config.max_daily_trades}) reached for today.",
                violation_code="MAX_DAILY_TRADES_REACHED",
            )

        # 4. CONCURRENT OPEN POSITIONS CHECK
        if len(self.current_open_positions) >= self.config.max_open_positions:
            return RiskEvaluationResult(
                approved=False,
                reason=f"Max open positions ({self.config.max_open_positions}) reached.",
                violation_code="MAX_OPEN_POSITIONS_REACHED",
            )

        # 5. DUPLICATE TRADE / EXISTING POSITION CHECK
        symbol = request.contract.trading_symbol
        if symbol in self.current_open_positions:
            return RiskEvaluationResult(
                approved=False,
                reason=f"Duplicate trade: Position already exists for {symbol}.",
                violation_code="DUPLICATE_POSITION_EXISTS",
            )

        # 6. MARKET HOURS VALIDATION
        if self.config.enforce_market_hours:
            # Weekend check (Saturday = 5, Sunday = 6)
            if eval_dt.weekday() >= 5:
                return RiskEvaluationResult(
                    approved=False,
                    reason="Market is closed on weekends (Saturday/Sunday).",
                    violation_code="MARKET_CLOSED_WEEKEND",
                )

            current_time = eval_dt.time()
            if current_time < self.config.market_open_time:
                return RiskEvaluationResult(
                    approved=False,
                    reason=f"Market not open yet ({current_time.strftime('%H:%M')} < {self.config.market_open_time.strftime('%H:%M')}).",
                    violation_code="MARKET_NOT_OPEN",
                )

            if current_time >= self.config.cutoff_time:
                return RiskEvaluationResult(
                    approved=False,
                    reason=f"Past intraday cutoff time ({current_time.strftime('%H:%M')} >= {self.config.cutoff_time.strftime('%H:%M')}).",
                    violation_code="AFTER_CUTOFF_TIME",
                )

        # 7. STALE DATA CHECK
        if request.quote_timestamp:
            age_seconds = (eval_dt - request.quote_timestamp).total_seconds()
            if age_seconds < 0:
                age_seconds = 0
            if age_seconds > self.config.max_data_age_seconds:
                return RiskEvaluationResult(
                    approved=False,
                    reason=f"Stale market data: Quote timestamp is {age_seconds:.1f}s old (max allowed: {self.config.max_data_age_seconds}s).",
                    violation_code="STALE_MARKET_DATA",
                )

        # 8. BID-ASK SPREAD VALIDATION
        if request.bid_price and request.ask_price and request.ask_price > 0:
            spread_pct = ((request.ask_price - request.bid_price) / request.ask_price) * 100.0
            if spread_pct > self.config.max_bid_ask_spread_pct:
                return RiskEvaluationResult(
                    approved=False,
                    reason=f"Wide bid-ask spread ({spread_pct:.2f}% > max {self.config.max_bid_ask_spread_pct}%). Illiquid contract.",
                    violation_code="WIDE_BID_ASK_SPREAD",
                )

        # 9. LOT SIZING & CAPITAL ENFORCEMENT
        if request.proposed_lots > self.config.max_lots_per_trade:
            return RiskEvaluationResult(
                approved=False,
                reason=f"Proposed lots ({request.proposed_lots}) exceed maximum allowed ({self.config.max_lots_per_trade}).",
                violation_code="EXCEEDS_MAX_LOTS",
            )

        total_quantity = request.proposed_lots * request.contract.lot_size
        allocated_capital = total_quantity * request.entry_price

        if allocated_capital > self.config.max_capital_per_trade:
            return RiskEvaluationResult(
                approved=False,
                reason=f"Required capital (₹{allocated_capital:.2f}) exceeds max per trade (₹{self.config.max_capital_per_trade:.2f}).",
                violation_code="EXCEEDS_MAX_CAPITAL",
            )

        # 10. MANDATORY STOP LOSS VALIDATION
        max_loss_risk = 0.0
        if self.config.enforce_stop_loss:
            if request.stop_loss is None or request.stop_loss <= 0:
                return RiskEvaluationResult(
                    approved=False,
                    reason="Mandatory stop-loss missing or invalid. Trade rejected by risk engine.",
                    violation_code="MISSING_STOP_LOSS",
                )

            if request.action == "BUY" and request.stop_loss >= request.entry_price:
                return RiskEvaluationResult(
                    approved=False,
                    reason=f"Invalid stop loss for BUY: SL (₹{request.stop_loss}) must be strictly below entry (₹{request.entry_price}).",
                    violation_code="INVALID_STOP_LOSS",
                )

            max_loss_risk = abs(request.entry_price - request.stop_loss) * total_quantity
            if max_loss_risk > self.config.max_loss_per_trade:
                return RiskEvaluationResult(
                    approved=False,
                    reason=f"Max loss risk (₹{max_loss_risk:.2f}) exceeds allowable limit per trade (₹{self.config.max_loss_per_trade:.2f}).",
                    violation_code="EXCEEDS_MAX_LOSS_PER_TRADE",
                )

        # ALL CHECKS PASSED -> APPROVE TRADE
        return RiskEvaluationResult(
            approved=True,
            reason="All deterministic risk and safety checks passed successfully.",
            allocated_lots=request.proposed_lots,
            allocated_capital=allocated_capital,
            max_loss_risk=max_loss_risk,
        )

    def record_trade_opened(
        self,
        symbol: str,
        lots: int,
        capital: float,
        entry_price: float,
        stop_loss: float,
    ) -> None:
        """Register newly opened position in risk engine state."""
        self.daily_trades_count += 1
        self.current_open_positions[symbol] = {
            "symbol": symbol,
            "lots": lots,
            "capital": capital,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "opened_at": datetime.now(),
        }
        logger.info(f"Position registered: {symbol} | Lots: {lots} | Daily Trades: {self.daily_trades_count}")

    def record_trade_closed(self, symbol: str, realized_pnl: float) -> None:
        """Register closed position and update daily cumulative PnL."""
        if symbol in self.current_open_positions:
            del self.current_open_positions[symbol]

        self.daily_realized_pnl += realized_pnl
        logger.info(
            f"Position closed: {symbol} | PnL: ₹{realized_pnl:.2f} | Cumulative Daily PnL: ₹{self.daily_realized_pnl:.2f}"
        )

        # Check circuit breaker
        if self.daily_realized_pnl <= -self.config.max_daily_loss:
            self.trigger_kill_switch(
                f"Cumulative daily loss (-₹{abs(self.daily_realized_pnl):.2f}) exceeded threshold (-₹{self.config.max_daily_loss:.2f})"
            )

