"""
End-to-End Indian F&O Pipeline Orchestrator and Validation Engine.
Glues together:
AI Signal -> Scrip Master -> Contract Resolver (Phase 1)
-> Deterministic Risk Engine (Phase 2)
-> Paper Trading Engine (Phase 3)
-> Live Tick Monitor & Alert Dispatcher (Phase 4)
-> Real-Time Market Data & TradingAgents Signal Integration (Phase 5).

ABSOLUTE SAFETY INVARIANT:
Guarantees zero real broker execution. Only virtual paper execution is permitted.
placeOrder(), modifyOrder(), cancelOrder() are NEVER called.
"""

import hashlib
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .contract_resolver import FOContractResolver
from .market_data import AngelOneMarketDataProvider, DataSource, MarketTick
from .models import (
    ContractSpec,
    DerivativeType,
    MarketBias,
    ResolutionRequest,
    ResolutionResult,
    StrikeMode,
)
from .paper_engine import FOPaperTradingEngine, PaperAccountStats, PaperOrder, PaperPosition
from .risk_engine import (
    DeterministicRiskEngine,
    RiskConfig,
    RiskEvaluationRequest,
    RiskEvaluationResult,
)
from .scrip_master import ScripMasterManager
from .signal_adapter import TradingAgentsSignalAdapter, ValidatedSignal

logger = logging.getLogger(__name__)


# =============================================================================
# 1. ORCHESTRATION MODELS
# =============================================================================
class PipelineExecutionResult(BaseModel):
    """Result of an end-to-end trade pipeline execution."""
    status: str  # EXECUTED_PAPER, CONTRACT_RESOLUTION_FAILED, RISK_REJECTED, EXECUTION_FAILED, SIGNAL_VALIDATION_FAILED, DATA_FEED_DISCONNECTED, STALE_TICK_REJECTED
    success: bool
    underlying: str
    action: str
    contract: Optional[ContractSpec] = None
    order: Optional[PaperOrder] = None
    risk_evaluation: Optional[RiskEvaluationResult] = None
    data_source: str = "LIVE_ANGEL_ONE"
    reason: str
    timestamp: datetime = Field(default_factory=datetime.now)


# =============================================================================
# 2. PHONE / NOTIFICATION ALERT DISPATCHER (READ-ONLY / SECURE)
# =============================================================================
class PhoneAlertDispatcher:
    """
    Dispatches real-time alerts to phone (Telegram / Webhook / Console).
    All alerts are clearly marked with [🟡 PAPER TRADE] to eliminate any confusion.
    Ensures zero secret leakage.
    """

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.is_configured = bool(self.bot_token and self.chat_id)

    def send_alert(self, title: str, message: str) -> bool:
        """Log alert and optionally send to Telegram if configured."""
        formatted = f"🔔 [🟡 PAPER TRADE: {title}]\n{message}"
        logger.info(formatted)

        if not self.is_configured:
            return True

        # In production, send via requests to Telegram Bot API
        try:
            import requests
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": formatted, "parse_mode": "Markdown"}
            resp = requests.post(url, json=payload, timeout=5)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to dispatch phone alert to Telegram: {e}")
            return False


# =============================================================================
# 3. UNIFIED PIPELINE ORCHESTRATOR
# =============================================================================
class FOPipelineOrchestrator:
    """
    Master controller for Indian F&O TradingAgents execution.
    Enforces the complete safety chain:
    Signal Validation -> Resolution -> Risk Approval -> Paper Execution -> Position Management -> Emergency Kill Switch.
    """

    def __init__(
        self,
        scrip_master: ScripMasterManager,
        risk_engine: Optional[DeterministicRiskEngine] = None,
        paper_engine: Optional[FOPaperTradingEngine] = None,
        alert_dispatcher: Optional[PhoneAlertDispatcher] = None,
        market_data_provider: Optional[AngelOneMarketDataProvider] = None,
        signal_adapter: Optional[TradingAgentsSignalAdapter] = None,
    ):
        self.scrip_master = scrip_master
        self.resolver = FOContractResolver(scrip_master=self.scrip_master)
        self.risk_engine = risk_engine or DeterministicRiskEngine()
        self.paper_engine = paper_engine or FOPaperTradingEngine()
        self.alert_dispatcher = alert_dispatcher or PhoneAlertDispatcher()
        self.market_data_provider = market_data_provider
        self.signal_adapter = signal_adapter or TradingAgentsSignalAdapter()

    def process_signal(
        self,
        underlying: Optional[str] = None,
        spot_price: Optional[float] = None,
        bias: Optional[MarketBias] = None,
        proposed_lots: int = 1,
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        strike_mode: StrikeMode = StrikeMode.ATM,
        strike_offset: int = 0,
        preferred_expiry: Optional[str] = None,
        evaluation_time: Optional[datetime] = None,
        strategy_name: str = "DEFAULT_STRATEGY",
        signal: Optional[ValidatedSignal] = None,
        data_source: str = "LIVE_ANGEL_ONE",
    ) -> PipelineExecutionResult:
        """
        Execute full pipeline from AI signal to virtual execution.
        Supports direct ValidatedSignal or explicit parameters.
        Guarantees FAIL-CLOSED semantics at every stage.
        """
        eval_dt = evaluation_time or datetime.now()

        # Step 0: Signal Validation (Phase 5)
        if signal is not None:
            is_valid, val_reason = self.signal_adapter.validate_signal(signal, evaluation_time=eval_dt)
            if not is_valid:
                logger.warning(f"Pipeline halted: Signal validation failed: {val_reason}")
                return PipelineExecutionResult(
                    status="SIGNAL_VALIDATION_FAILED",
                    success=False,
                    underlying=signal.underlying,
                    action="BUY",
                    data_source=signal.data_source,
                    reason=val_reason,
                )
            underlying = signal.underlying
            bias = signal.direction
            data_source = signal.data_source
            strategy_name = signal.strategy_id

        if not underlying or not bias or spot_price is None:
            return PipelineExecutionResult(
                status="SIGNAL_VALIDATION_FAILED",
                success=False,
                underlying=underlying or "UNKNOWN",
                action="BUY",
                data_source=data_source,
                reason="Missing required underlying, bias, or spot_price.",
            )

        # Step 0.5: Live Market Data Health Check (Phase 5)
        if self.market_data_provider is not None:
            if not self.market_data_provider.is_connected():
                logger.warning("Pipeline halted: Live market data feed is DISCONNECTED. Fail-closed.")
                return PipelineExecutionResult(
                    status="DATA_FEED_DISCONNECTED",
                    success=False,
                    underlying=underlying,
                    action="BUY",
                    data_source=data_source,
                    reason="Angel One live market data stream is disconnected. Fail-closed: NO TRADE.",
                )

        # Step 1: Dynamic F&O Contract Resolution (Phase 1)
        res_req = ResolutionRequest(
            underlying=underlying,
            spot_price=spot_price,
            bias=bias,
            instrument_type=DerivativeType.OPTIDX,
            strike_mode=strike_mode,
            strike_offset=strike_offset,
            preferred_expiry=preferred_expiry,
        )
        res_result = self.resolver.resolve(res_req)

        if not res_result.success or not res_result.contract:
            err_reason = res_result.error_reason or "Unknown contract resolution failure"
            logger.warning(f"Pipeline halted: Contract resolution failed for {underlying}: {err_reason}")
            return PipelineExecutionResult(
                status="CONTRACT_RESOLUTION_FAILED",
                success=False,
                underlying=underlying,
                action="BUY",
                data_source=data_source,
                reason=err_reason,
            )

        contract = res_result.contract

        # Step 1.5: Tick Freshness Check for resolved contract
        quote_timestamp = eval_dt
        bid_price = None
        ask_price = None

        if self.market_data_provider is not None:
            tick = self.market_data_provider.get_latest_tick(contract.symbol_token)
            if tick is not None:
                if tick.is_stale:
                    logger.warning(f"Pipeline halted: Live market tick for {contract.trading_symbol} is STALE.")
                    return PipelineExecutionResult(
                        status="STALE_TICK_REJECTED",
                        success=False,
                        underlying=underlying,
                        action="BUY",
                        contract=contract,
                        data_source=data_source,
                        reason=f"Market tick for token {contract.symbol_token} is stale. NO NEW TRADE.",
                    )
                entry_price = tick.ltp
                quote_timestamp = tick.timestamp
                bid_price = tick.bid
                ask_price = tick.ask

        resolved_entry_price = entry_price or spot_price * 0.01

        # Default SL and Target if not explicitly provided (Safety guardrail)
        resolved_sl = stop_loss
        if resolved_sl is None and self.risk_engine.config.enforce_stop_loss:
            resolved_sl = round(resolved_entry_price * 0.75, 2)

        # Step 2: Deterministic Risk Engine Evaluation (Phase 2)
        risk_req = RiskEvaluationRequest(
            contract=contract,
            action="BUY",
            proposed_lots=proposed_lots,
            entry_price=resolved_entry_price,
            stop_loss=resolved_sl,
            target=target,
            evaluation_time=eval_dt,
            quote_timestamp=quote_timestamp,
            bid_price=bid_price,
            ask_price=ask_price,
        )
        risk_result = self.risk_engine.evaluate(risk_req)

        if not risk_result.approved:
            logger.warning(f"Pipeline halted: Risk rejected trade for {contract.trading_symbol}: {risk_result.reason}")
            return PipelineExecutionResult(
                status="RISK_REJECTED",
                success=False,
                underlying=underlying,
                action="BUY",
                contract=contract,
                risk_evaluation=risk_result,
                data_source=data_source,
                reason=risk_result.reason,
            )

        # Step 3: Idempotency Key Generation
        date_str = eval_dt.strftime("%Y%m%d")
        time_block = eval_dt.hour * 4 + eval_dt.minute // 15
        idemp_raw = f"{strategy_name}_{contract.trading_symbol}_{date_str}_{time_block}_BUY_{data_source}"
        idempotency_key = hashlib.sha256(idemp_raw.encode("utf-8")).hexdigest()[:24]

        # Step 4: Paper Trading Execution (Phase 3 & 5)
        order = self.paper_engine.submit_order(
            contract=contract,
            action="BUY",
            lots=risk_result.allocated_lots,
            current_price=resolved_entry_price,
            stop_loss=resolved_sl,
            target=target,
            idempotency_key=idempotency_key,
            data_source=data_source,
        )

        if order.status != "FILLED":
            return PipelineExecutionResult(
                status="EXECUTION_FAILED",
                success=False,
                underlying=underlying,
                action="BUY",
                contract=contract,
                order=order,
                risk_evaluation=risk_result,
                data_source=data_source,
                reason=f"Paper order execution rejected: {order.reason}",
            )

        # Step 5: Update Risk Engine State with new open position
        self.risk_engine.record_trade_opened(
            symbol=contract.trading_symbol,
            lots=order.lots,
            capital=risk_result.allocated_capital,
            entry_price=order.fill_price,
            stop_loss=resolved_sl or 0.0,
        )

        # Step 6: Dispatch Alert (Clearly labeled as [🟡 PAPER TRADE])
        self.alert_dispatcher.send_alert(
            title="TRADE EXECUTED",
            message=(
                f"*Instrument:* `{contract.trading_symbol}`\n"
                f"*Action:* `BUY` | *Lots:* `{order.lots}` ({order.quantity} qty)\n"
                f"*Fill Price:* `₹{order.fill_price:.2f}`\n"
                f"*Stop Loss:* `₹{resolved_sl}` | *Target:* `₹{target or 'OPEN'}`\n"
                f"*Charges:* `₹{order.charges:.2f}`\n"
                f"*Data Source:* `{data_source}`"
            ),
        )

        return PipelineExecutionResult(
            status="EXECUTED_PAPER",
            success=True,
            underlying=underlying,
            action="BUY",
            contract=contract,
            order=order,
            risk_evaluation=risk_result,
            data_source=data_source,
            reason="Trade approved by Risk Engine and executed in Paper Ledger.",
        )

    def on_market_tick(self, symbol: str, ltp: float) -> Optional[Dict[str, Any]]:
        """
        Feed live market tick into paper engine to update MTM and check SL/Target triggers.
        Automatically pauses automated exits if data provider is disconnected.
        """
        # Connection check
        if self.market_data_provider is not None:
            if not self.market_data_provider.is_connected():
                self.paper_engine.mark_positions_unknown_data(True)
            else:
                self.paper_engine.mark_positions_unknown_data(False)

        update_info = self.paper_engine.update_mark_price(symbol=symbol, ltp=ltp)
        if not update_info:
            return None

        triggered = update_info.get("triggered_exit")
        if triggered:
            # Inform Risk Engine that trade closed
            stats = self.paper_engine.get_performance_metrics()
            self.risk_engine.record_trade_closed(symbol=symbol, realized_pnl=update_info.get("unrealized_pnl", 0.0))

            self.alert_dispatcher.send_alert(
                title=f"AUTO EXIT: {triggered}",
                message=(
                    f"*Symbol:* `{symbol}`\n"
                    f"*Exit LTP:* `₹{ltp:.2f}`\n"
                    f"*Trigger Reason:* `{triggered}`\n"
                    f"*Current Net PnL:* `₹{stats.net_realized_pnl:.2f}`"
                ),
            )

        return update_info

    def emergency_stop_all(self, reason: str = "EMERGENCY_KILL_SWITCH_TRIGGERED") -> Dict[str, Any]:
        """
        Emergency kill switch:
        1. Activates Risk Engine lock (no further trades allowed).
        2. Closes all open PAPER positions immediately.
        3. Real Angel One account is UNTOUCHED.
        4. Dispatches high-priority alert.
        """
        logger.critical(f"EMERGENCY STOP TRIGGERED: {reason}")
        self.risk_engine.trigger_kill_switch(reason=reason)

        open_positions = self.paper_engine.get_open_positions()
        closed_records = []

        for pos in open_positions:
            order = self.paper_engine.close_position(
                symbol=pos.symbol,
                exit_price=pos.current_mark,
                reason=f"EMERGENCY_STOP: {reason}",
            )
            if order:
                closed_records.append(pos.symbol)

        stats = self.paper_engine.get_performance_metrics()

        self.alert_dispatcher.send_alert(
            title="EMERGENCY STOP ACTIVATED",
            message=(
                f"*Reason:* `{reason}`\n"
                f"*Closed Paper Positions:* `{len(closed_records)}` ({', '.join(closed_records) if closed_records else 'None'})\n"
                f"*Current Cash:* `₹{stats.current_cash:.2f}`\n"
                f"*Net PnL:* `₹{stats.net_realized_pnl:.2f}`\n"
                f"*Note:* Real Angel One broker account was NOT touched."
            ),
        )

        return {
            "status": "EMERGENCY_LOCKED",
            "reason": reason,
            "closed_positions_count": len(closed_records),
            "closed_symbols": closed_records,
            "final_net_pnl": stats.net_realized_pnl,
            "real_broker_touched": False,
        }

    def generate_validation_report(self) -> str:
        """Generate formatted validation scorecard report with Live vs Sim distinction."""
        stats = self.paper_engine.get_performance_metrics()
        open_pos = self.paper_engine.get_open_positions()

        lines = [
            "# 📈 Indian F&O Paper Trading Validation Report",
            f"**Generated At:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}",
            f"**Engine Status:** {'🔴 EMERGENCY LOCKED' if self.risk_engine.is_emergency_locked else '🟢 ACTIVE'}",
            f"**Data Provider:** {'🟢 CONNECTED' if (self.market_data_provider and self.market_data_provider.is_connected()) else '⚪ STANDALONE / SIMULATED'}",
            "",
            "## 1. Portfolio & Capital Summary",
            f"- **Initial Capital:** ₹{stats.initial_capital:,.2f}",
            f"- **Current Cash:** ₹{stats.current_cash:,.2f}",
            f"- **Total Portfolio Value:** ₹{stats.portfolio_value:,.2f}",
            f"- **Net Realized P&L:** ₹{stats.net_realized_pnl:,.2f}",
            f"- **Unrealized MTM:** ₹{stats.unrealized_mtm:,.2f}",
            f"- **Total Charges & Taxes Paid:** ₹{stats.total_charges_paid:,.2f}",
            "",
            "## 2. Performance Metrics & Scorecard",
            f"- **Total Completed Trades:** {stats.total_trades} (Live Data: {stats.live_trades_count} | Simulation: {stats.sim_trades_count})",
            f"- **Winning Trades:** {stats.winning_trades}",
            f"- **Losing Trades:** {stats.losing_trades}",
            f"- **Win Rate:** {stats.win_rate_pct:.2f}%",
            f"- **Profit Factor:** {stats.profit_factor:.2f}",
            f"- **Max Drawdown:** ₹{stats.max_drawdown_amount:,.2f} ({stats.max_drawdown_pct:.2f}%)",
            f"- **Max Consecutive Losses:** {stats.max_consecutive_losses}",
            "",
            "## 3. Active Open Positions",
        ]

        if not open_pos:
            lines.append("_No open positions currently._")
        else:
            lines.append("| Symbol | Lots | Qty | Entry Price | Mark Price | Unrealized P&L | Stop Loss | Target | Source | Data Status |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for p in open_pos:
                status_str = "⚠️ UNCERTAIN (PAUSED)" if p.is_unknown_data else "🟢 OK"
                lines.append(
                    f"| `{p.symbol}` | {p.lots} | {p.quantity} | ₹{p.entry_price:.2f} | ₹{p.current_mark:.2f} | ₹{p.unrealized_pnl:.2f} | ₹{p.stop_loss or '-'} | ₹{p.target or '-'} | `{p.data_source}` | {status_str} |"
                )

        return "\n".join(lines)
