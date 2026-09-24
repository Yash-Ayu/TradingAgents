"""
Indian F&O Paper Trading Engine and Virtual Execution Ledger.
Simulates realistic order execution with slippage, Indian statutory taxes/charges (STT, GST, SEBI, Stamp Duty, Brokerage),
real-time MTM position tracking, stop-loss/target triggers, and SQLite persistence.

ABSOLUTE SAFETY INVARIANT:
This module executes strictly in-memory or in a local SQLite ledger.
No real orders are ever transmitted to Angel One or any exchange.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from .models import ContractSpec, DerivativeType

logger = logging.getLogger(__name__)


# =============================================================================
# 1. INDIAN F&O STATUTORY CHARGES CALCULATOR
# =============================================================================
class IndianFOChargesCalculator:
    """
    Calculates realistic transaction costs for Indian derivatives:
    - Brokerage: Flat ₹20 per order
    - STT (Securities Transaction Tax): 0.1% on options sell premium; 0.02% on futures sell
    - Exchange Turnover Charges: ~0.05% on options premium turnover
    - GST: 18% on (Brokerage + Exchange Turnover)
    - SEBI Charges: ₹10 per crore (0.0001%)
    - Stamp Duty: 0.003% on buy side premium turnover
    """

    BROKERAGE_PER_ORDER = 20.0
    EXCHANGE_TURNOVER_RATE = 0.0005      # 0.05%
    GST_RATE = 0.18                      # 18%
    SEBI_RATE = 0.000001                 # ₹10 per crore
    STAMP_DUTY_BUY_RATE = 0.00003        # 0.003%
    STT_OPTIONS_SELL_RATE = 0.001        # 0.1%
    STT_FUTURES_SELL_RATE = 0.0002       # 0.02%

    @classmethod
    def calculate(
        cls,
        action: str,
        instrument_type: DerivativeType,
        price: float,
        quantity: int,
    ) -> Dict[str, float]:
        turnover = price * quantity
        brokerage = cls.BROKERAGE_PER_ORDER
        exchange_turnover = turnover * cls.EXCHANGE_TURNOVER_RATE
        gst = (brokerage + exchange_turnover) * cls.GST_RATE
        sebi_charges = turnover * cls.SEBI_RATE

        # STT applies only on SELL
        stt = 0.0
        if action.upper() == "SELL":
            if instrument_type == DerivativeType.OPTIDX or instrument_type == DerivativeType.OPTSTK:
                stt = turnover * cls.STT_OPTIONS_SELL_RATE
            else:
                stt = turnover * cls.STT_FUTURES_SELL_RATE

        # Stamp duty applies only on BUY
        stamp_duty = 0.0
        if action.upper() == "BUY":
            stamp_duty = turnover * cls.STAMP_DUTY_BUY_RATE

        total = brokerage + exchange_turnover + gst + sebi_charges + stt + stamp_duty

        return {
            "turnover": round(turnover, 2),
            "brokerage": round(brokerage, 2),
            "stt": round(stt, 2),
            "exchange_turnover": round(exchange_turnover, 2),
            "gst": round(gst, 2),
            "sebi_charges": round(sebi_charges, 2),
            "stamp_duty": round(stamp_duty, 2),
            "total_charges": round(total, 2),
        }


# =============================================================================
# 2. PAPER MODELS & SCHEMAS
# =============================================================================
class PaperOrder(BaseModel):
    order_id: str
    idempotency_key: str
    contract: ContractSpec
    action: str
    lots: int
    quantity: int
    order_price: float
    fill_price: float
    slippage: float
    charges: float
    status: str  # FILLED, REJECTED, CANCELLED
    timestamp: datetime
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    data_source: str = "LIVE_ANGEL_ONE"
    reason: Optional[str] = None


class PaperPosition(BaseModel):
    symbol: str
    contract: ContractSpec
    lots: int
    quantity: int
    entry_price: float
    current_mark: float
    unrealized_pnl: float
    realized_pnl: float = 0.0
    total_charges: float = 0.0
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    highest_price: float
    lowest_price: float
    opened_at: datetime
    closed_at: Optional[datetime] = None
    data_source: str = "LIVE_ANGEL_ONE"
    is_unknown_data: bool = False


class PaperAccountStats(BaseModel):
    initial_capital: float
    current_cash: float
    portfolio_value: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    total_charges_paid: float
    net_realized_pnl: float
    unrealized_mtm: float
    peak_portfolio_value: float
    max_drawdown_amount: float
    max_drawdown_pct: float
    consecutive_losses: int
    max_consecutive_losses: int
    live_trades_count: int = 0
    sim_trades_count: int = 0
    data_failures_count: int = 0
    websocket_disconnects_count: int = 0


# =============================================================================
# 3. F&O PAPER TRADING ENGINE
# =============================================================================
class FOPaperTradingEngine:
    """
    Thread-safe, restart-safe Paper Trading Engine for Indian F&O.
    Backed by SQLite for robust order idempotency and audit trails.
    """

    def __init__(self, db_path: str | Path = ":memory:", initial_capital: float = 200000.0):
        self.initial_capital = initial_capital
        self.lock = threading.RLock()
        self.db_path = str(db_path)

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        """Create tables for paper trading ledger."""
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    order_price REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    slippage REAL NOT NULL,
                    charges REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    data_source TEXT DEFAULT 'LIVE_ANGEL_ONE'
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    symbol TEXT PRIMARY KEY,
                    contract_json TEXT NOT NULL,
                    lots INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    current_mark REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    total_charges REAL NOT NULL,
                    stop_loss REAL,
                    target REAL,
                    highest_price REAL NOT NULL,
                    lowest_price REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    data_source TEXT DEFAULT 'LIVE_ANGEL_ONE',
                    is_unknown_data INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS paper_trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    lots INTEGER NOT NULL,
                    gross_pnl REAL NOT NULL,
                    charges REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    data_source TEXT DEFAULT 'LIVE_ANGEL_ONE'
                );
                """
            )

            # Initialize account state if not present
            defaults = {
                "cash": self.initial_capital,
                "peak_portfolio_value": self.initial_capital,
                "max_drawdown_amount": 0.0,
                "max_drawdown_pct": 0.0,
                "consecutive_losses": 0,
                "max_consecutive_losses": 0,
            }
            for k, v in defaults.items():
                self.conn.execute("INSERT OR IGNORE INTO account_state VALUES (?, ?)", (k, json.dumps(v)))

    def _get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM account_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_state(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO account_state VALUES (?, ?)", (key, json.dumps(value)))

    def submit_order(
        self,
        contract: ContractSpec,
        action: str,
        lots: int,
        current_price: float,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        idempotency_key: Optional[str] = None,
        slippage_pct: float = 0.5,
        data_source: str = "LIVE_ANGEL_ONE",
    ) -> PaperOrder:
        """
        Simulate order execution with slippage and charges.
        Guarantees duplicate order rejection via idempotency key.
        """
        with self.lock:
            act_upper = action.upper().strip()
            symbol = contract.trading_symbol
            quantity = lots * contract.lot_size
            order_id = f"ORD_{uuid4().hex[:12]}"
            idemp_key = idempotency_key or f"IDEMP_{uuid4().hex}"

            # 1. IDEMPOTENCY / DUPLICATE CHECK
            existing = self.conn.execute(
                "SELECT order_id, status FROM paper_orders WHERE idempotency_key=?", (idemp_key,)
            ).fetchone()
            if existing:
                logger.warning(f"Duplicate order blocked by idempotency key: {idemp_key}")
                return PaperOrder(
                    order_id=existing["order_id"],
                    idempotency_key=idemp_key,
                    contract=contract,
                    action=act_upper,
                    lots=lots,
                    quantity=quantity,
                    order_price=current_price,
                    fill_price=0.0,
                    slippage=0.0,
                    charges=0.0,
                    status="REJECTED",
                    timestamp=datetime.now(),
                    data_source=data_source,
                    reason="DUPLICATE_ORDER_IDEMPOTENT_BLOCK",
                )

            # 2. SLIPPAGE CALCULATION
            slippage_amount = round(current_price * (slippage_pct / 100.0), 2)
            if act_upper == "BUY":
                fill_price = round(current_price + slippage_amount, 2)
            else:
                fill_price = round(max(0.05, current_price - slippage_amount), 2)

            # 3. CHARGES CALCULATION
            charges_dict = IndianFOChargesCalculator.calculate(
                action=act_upper,
                instrument_type=contract.instrument_type,
                price=fill_price,
                quantity=quantity,
            )
            total_charges = charges_dict["total_charges"]
            order_cost = (fill_price * quantity) + total_charges

            # 4. CASH AVAILABILITY CHECK (FOR BUY)
            current_cash = float(self._get_state("cash", self.initial_capital))
            if act_upper == "BUY" and order_cost > current_cash:
                logger.error(f"Insufficient cash for paper order: Required ₹{order_cost:.2f}, Available ₹{current_cash:.2f}")
                rej_order = PaperOrder(
                    order_id=order_id,
                    idempotency_key=idemp_key,
                    contract=contract,
                    action=act_upper,
                    lots=lots,
                    quantity=quantity,
                    order_price=current_price,
                    fill_price=0.0,
                    slippage=0.0,
                    charges=0.0,
                    status="REJECTED",
                    timestamp=datetime.now(),
                    data_source=data_source,
                    reason="INSUFFICIENT_FUNDS",
                )
                self.conn.execute(
                    """
                    INSERT INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        rej_order.order_id,
                        rej_order.idempotency_key,
                        rej_order.timestamp.isoformat(),
                        symbol,
                        act_upper,
                        lots,
                        quantity,
                        current_price,
                        0.0,
                        0.0,
                        0.0,
                        "REJECTED",
                        "INSUFFICIENT_FUNDS",
                        data_source,
                    ),
                )
                return rej_order

            # 5. EXECUTE FILL & UPDATE LEDGER
            new_cash = current_cash - order_cost if act_upper == "BUY" else current_cash + (fill_price * quantity) - total_charges
            self._set_state("cash", new_cash)

            order = PaperOrder(
                order_id=order_id,
                idempotency_key=idemp_key,
                contract=contract,
                action=act_upper,
                lots=lots,
                quantity=quantity,
                order_price=current_price,
                fill_price=fill_price,
                slippage=slippage_amount,
                charges=total_charges,
                status="FILLED",
                timestamp=datetime.now(),
                stop_loss=stop_loss,
                target=target,
                data_source=data_source,
                reason="SUCCESS",
            )

            # Record in orders table
            self.conn.execute(
                """
                INSERT INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order.order_id,
                    order.idempotency_key,
                    order.timestamp.isoformat(),
                    symbol,
                    act_upper,
                    lots,
                    quantity,
                    current_price,
                    fill_price,
                    slippage_amount,
                    total_charges,
                    "FILLED",
                    "SUCCESS",
                    data_source,
                ),
            )

            # Record or update position
            if act_upper == "BUY":
                self.conn.execute(
                    """
                    INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        symbol,
                        contract.model_dump_json(),
                        lots,
                        quantity,
                        fill_price,
                        fill_price,
                        0.0,
                        total_charges,
                        stop_loss,
                        target,
                        fill_price,
                        fill_price,
                        order.timestamp.isoformat(),
                        data_source,
                        0,
                    ),
                )
                logger.info(f"[PAPER FILL] BUY {lots} lots ({quantity} qty) of {symbol} @ ₹{fill_price:.2f} (Charges: ₹{total_charges:.2f}, Source: {data_source})")

            return order

    def mark_positions_unknown_data(self, is_unknown: bool = True) -> None:
        """Mark open positions with uncertain/unknown data state when live feed disconnects."""
        with self.lock:
            self.conn.execute("UPDATE paper_positions SET is_unknown_data=?", (1 if is_unknown else 0,))
            logger.warning(
                f"Positions is_unknown_data set to {is_unknown}. Automated exits {'PAUSED' if is_unknown else 'RESUMED'}."
            )

    def update_mark_price(self, symbol: str, ltp: float) -> Optional[Dict[str, Any]]:
        """
        Update mark-to-market price for an open position.
        Checks for automated Stop-Loss or Target trigger unless data feed is unknown/disconnected.
        """
        with self.lock:
            row = self.conn.execute("SELECT * FROM paper_positions WHERE symbol=?", (symbol,)).fetchone()
            if not row:
                return None

            entry_price = row["entry_price"]
            quantity = row["quantity"]
            unrealized_pnl = round((ltp - entry_price) * quantity, 2)
            highest_price = max(row["highest_price"], ltp)
            lowest_price = min(row["lowest_price"], ltp)
            stop_loss = row["stop_loss"]
            target = row["target"]
            is_unknown = bool(row["is_unknown_data"])

            self.conn.execute(
                """
                UPDATE paper_positions
                SET current_mark=?, unrealized_pnl=?, highest_price=?, lowest_price=?
                WHERE symbol=?
                """,
                (ltp, unrealized_pnl, highest_price, lowest_price, symbol),
            )

            # Automated Exit Checks (Paused if market data is unknown/disconnected)
            if is_unknown:
                logger.warning(f"[DATA UNCERTAIN] Automated SL/Target check PAUSED for {symbol} (Feed disconnected).")
                return {
                    "symbol": symbol,
                    "ltp": ltp,
                    "unrealized_pnl": unrealized_pnl,
                    "triggered_exit": None,
                    "data_status": "UNKNOWN_DATA_PAUSED",
                }

            triggered_exit = None
            if stop_loss and ltp <= stop_loss:
                logger.warning(f"[AUTO STOP-LOSS TRIGGER] {symbol}: LTP ₹{ltp:.2f} <= SL ₹{stop_loss:.2f}")
                self.close_position(symbol=symbol, exit_price=ltp, reason="STOP_LOSS_HIT")
                triggered_exit = "STOP_LOSS_HIT"
            elif target and ltp >= target:
                logger.info(f"[AUTO TARGET TRIGGER] {symbol}: LTP ₹{ltp:.2f} >= Target ₹{target:.2f}")
                self.close_position(symbol=symbol, exit_price=ltp, reason="TARGET_HIT")
                triggered_exit = "TARGET_HIT"

            return {
                "symbol": symbol,
                "ltp": ltp,
                "unrealized_pnl": unrealized_pnl,
                "triggered_exit": triggered_exit,
            }

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        reason: str = "MANUAL_CLOSE",
        slippage_pct: float = 0.5,
    ) -> Optional[PaperOrder]:
        """Close an existing open position, record realized PnL, and update metrics."""
        with self.lock:
            row = self.conn.execute("SELECT * FROM paper_positions WHERE symbol=?", (symbol,)).fetchone()
            if not row:
                logger.warning(f"Attempted to close non-existent position: {symbol}")
                return None

            contract_dict = json.loads(row["contract_json"])
            contract = ContractSpec(**contract_dict)
            lots = row["lots"]
            quantity = row["quantity"]
            entry_price = row["entry_price"]
            entry_time = row["opened_at"]
            entry_charges = row["total_charges"]
            data_source = row["data_source"] if "data_source" in row.keys() else "LIVE_ANGEL_ONE"

            # Slippage on exit (SELL)
            slippage_amount = round(exit_price * (slippage_pct / 100.0), 2)
            fill_price = round(max(0.05, exit_price - slippage_amount), 2)

            # Exit charges
            exit_charges_dict = IndianFOChargesCalculator.calculate(
                action="SELL",
                instrument_type=contract.instrument_type,
                price=fill_price,
                quantity=quantity,
            )
            exit_charges = exit_charges_dict["total_charges"]
            total_trade_charges = round(entry_charges + exit_charges, 2)

            gross_pnl = round((fill_price - entry_price) * quantity, 2)
            net_pnl = round(gross_pnl - total_trade_charges, 2)

            # Update cash
            current_cash = float(self._get_state("cash", self.initial_capital))
            new_cash = round(current_cash + (fill_price * quantity) - exit_charges, 2)
            self._set_state("cash", new_cash)

            # Remove from open positions
            self.conn.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))

            # Record in trade history
            now_iso = datetime.now().isoformat()
            self.conn.execute(
                """
                INSERT INTO paper_trade_history (symbol, entry_price, exit_price, quantity, lots, gross_pnl, charges, net_pnl, entry_time, exit_time, exit_reason, data_source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    symbol,
                    entry_price,
                    fill_price,
                    quantity,
                    lots,
                    gross_pnl,
                    total_trade_charges,
                    net_pnl,
                    entry_time,
                    now_iso,
                    reason,
                    data_source,
                ),
            )

            # Update Drawdown & Consecutive Loss tracking
            consecutive_losses = int(self._get_state("consecutive_losses", 0))
            max_consecutive_losses = int(self._get_state("max_consecutive_losses", 0))
            if net_pnl < 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0

            self._set_state("consecutive_losses", consecutive_losses)
            self._set_state("max_consecutive_losses", max_consecutive_losses)

            # Record exit order in orders table
            order_id = f"ORD_{uuid4().hex[:12]}"
            idemp_key = f"EXIT_{uuid4().hex}"
            self.conn.execute(
                """
                INSERT INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order_id,
                    idemp_key,
                    now_iso,
                    symbol,
                    "SELL",
                    lots,
                    quantity,
                    exit_price,
                    fill_price,
                    slippage_amount,
                    exit_charges,
                    "FILLED",
                    reason,
                    data_source,
                ),
            )

            logger.info(
                f"[PAPER CLOSE] {symbol} @ ₹{fill_price:.2f} | Gross: ₹{gross_pnl:.2f} | Charges: ₹{total_trade_charges:.2f} | Net: ₹{net_pnl:.2f} ({reason}, Source: {data_source})"
            )

            return PaperOrder(
                order_id=order_id,
                idempotency_key=idemp_key,
                contract=contract,
                action="SELL",
                lots=lots,
                quantity=quantity,
                order_price=exit_price,
                fill_price=fill_price,
                slippage=slippage_amount,
                charges=exit_charges,
                status="FILLED",
                timestamp=datetime.now(),
                data_source=data_source,
                reason=reason,
            )

    def get_open_positions(self) -> List[PaperPosition]:
        """Fetch all currently open positions."""
        with self.lock:
            rows = self.conn.execute("SELECT * FROM paper_positions").fetchall()
            positions = []
            for r in rows:
                contract = ContractSpec(**json.loads(r["contract_json"]))
                data_source = r["data_source"] if "data_source" in r.keys() else "LIVE_ANGEL_ONE"
                is_unknown = bool(r["is_unknown_data"]) if "is_unknown_data" in r.keys() else False
                positions.append(
                    PaperPosition(
                        symbol=r["symbol"],
                        contract=contract,
                        lots=r["lots"],
                        quantity=r["quantity"],
                        entry_price=r["entry_price"],
                        current_mark=r["current_mark"],
                        unrealized_pnl=r["unrealized_pnl"],
                        total_charges=r["total_charges"],
                        stop_loss=r["stop_loss"],
                        target=r["target"],
                        highest_price=r["highest_price"],
                        lowest_price=r["lowest_price"],
                        opened_at=datetime.fromisoformat(r["opened_at"]),
                        data_source=data_source,
                        is_unknown_data=is_unknown,
                    )
                )
            return positions

    def get_performance_metrics(self) -> PaperAccountStats:
        """Calculate complete performance scorecard with live vs sim distinction."""
        with self.lock:
            cash = float(self._get_state("cash", self.initial_capital))
            rows = self.conn.execute("SELECT * FROM paper_trade_history").fetchall()
            open_pos = self.get_open_positions()

            unrealized_mtm = sum(p.unrealized_pnl for p in open_pos)
            portfolio_value = round(cash + sum(p.current_mark * p.quantity for p in open_pos), 2)

            # Drawdown
            peak_val = float(self._get_state("peak_portfolio_value", self.initial_capital))
            peak_val = max(peak_val, portfolio_value)
            self._set_state("peak_portfolio_value", peak_val)

            dd_amount = round(peak_val - portfolio_value, 2)
            dd_pct = round((dd_amount / peak_val) * 100.0 if peak_val > 0 else 0.0, 2)

            max_dd_amount = max(float(self._get_state("max_drawdown_amount", 0.0)), dd_amount)
            max_dd_pct = max(float(self._get_state("max_drawdown_pct", 0.0)), dd_pct)
            self._set_state("max_drawdown_amount", max_dd_amount)
            self._set_state("max_drawdown_pct", max_dd_pct)

            total_trades = len(rows)
            winning_trades = sum(1 for r in rows if r["net_pnl"] > 0)
            losing_trades = sum(1 for r in rows if r["net_pnl"] <= 0)
            win_rate = round((winning_trades / total_trades) * 100.0 if total_trades > 0 else 0.0, 2)

            gross_profit = sum(r["gross_pnl"] for r in rows if r["gross_pnl"] > 0)
            gross_loss = abs(sum(r["gross_pnl"] for r in rows if r["gross_pnl"] < 0))
            profit_factor = round(gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0), 2)

            total_charges_paid = round(sum(r["charges"] for r in rows), 2)
            net_realized_pnl = round(sum(r["net_pnl"] for r in rows), 2)

            # Live vs Sim breakdown
            live_trades = sum(1 for r in rows if ("data_source" in r.keys() and r["data_source"] == "LIVE_ANGEL_ONE"))
            sim_trades = sum(1 for r in rows if ("data_source" in r.keys() and r["data_source"] == "SIMULATION"))

            return PaperAccountStats(
                initial_capital=self.initial_capital,
                current_cash=round(cash, 2),
                portfolio_value=portfolio_value,
                total_trades=total_trades,
                winning_trades=winning_trades,
                losing_trades=losing_trades,
                win_rate_pct=win_rate,
                gross_profit=round(gross_profit, 2),
                gross_loss=round(gross_loss, 2),
                profit_factor=profit_factor,
                total_charges_paid=total_charges_paid,
                net_realized_pnl=net_realized_pnl,
                unrealized_mtm=round(unrealized_mtm, 2),
                peak_portfolio_value=round(peak_val, 2),
                max_drawdown_amount=max_dd_amount,
                max_drawdown_pct=max_dd_pct,
                consecutive_losses=int(self._get_state("consecutive_losses", 0)),
                max_consecutive_losses=int(self._get_state("max_consecutive_losses", 0)),
                live_trades_count=live_trades,
                sim_trades_count=sim_trades,
            )

