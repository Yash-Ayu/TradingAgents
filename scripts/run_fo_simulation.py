"""
Live End-to-End Simulation Script for Indian F&O Trading Pipeline.
Demonstrates:
AI Signal -> Scrip Master -> Contract Resolver -> Deterministic Risk Engine
-> Paper Ledger Execution -> Tick Update -> Auto Target Exit -> Validation Scorecard.

ABSOLUTE SAFETY INVARIANT:
Guarantees 100% paper execution. No real orders are ever transmitted to Angel One.
"""

import logging
import sys
from pathlib import Path

# Ensure tradingagents is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tradingagents.integrations.angel_one.models import MarketBias, StrikeMode
from tradingagents.integrations.angel_one.orchestrator import (
    FOPipelineOrchestrator,
    PhoneAlertDispatcher,
)
from tradingagents.integrations.angel_one.paper_engine import FOPaperTradingEngine
from tradingagents.integrations.angel_one.risk_engine import (
    DeterministicRiskEngine,
    RiskConfig,
)
from tradingagents.integrations.angel_one.scrip_master import ScripMasterManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FOSimulation")


def run_simulation():
    logger.info("=== STARTING INDIAN F&O END-TO-END PIPELINE SIMULATION ===")

    # 1. Initialize Components
    logger.info("1. Initializing Scrip Master and Engines...")
    scrip_mgr = ScripMasterManager()
    scrip_mgr.ensure_loaded()

    # Risk config with test market hours disabled so simulation runs anytime
    risk_config = RiskConfig(
        max_capital_per_trade=50000.0,
        max_lots_per_trade=2,
        max_open_positions=2,
        enforce_market_hours=False,
        enforce_stop_loss=True,
    )
    risk_engine = DeterministicRiskEngine(config=risk_config)
    paper_engine = FOPaperTradingEngine(db_path=":memory:", initial_capital=100000.0)
    alert_dispatcher = PhoneAlertDispatcher()

    orchestrator = FOPipelineOrchestrator(
        scrip_master=scrip_mgr,
        risk_engine=risk_engine,
        paper_engine=paper_engine,
        alert_dispatcher=alert_dispatcher,
    )

    # 2. Simulate AI Signal (NIFTY Bullish @ Spot 25000)
    logger.info("\n2. Simulating AI Signal: NIFTY BULLISH @ Spot 25,000.0 ...")
    exec_result = orchestrator.process_signal(
        underlying="NIFTY",
        spot_price=25000.0,
        bias=MarketBias.BULLISH,
        proposed_lots=1,
        entry_price=180.0,
        stop_loss=140.0,
        target=230.0,
        strike_mode=StrikeMode.ATM,
        strategy_name="SMC_ORDER_BLOCK_LONG",
    )

    logger.info(f"   Execution Status: {exec_result.status}")
    logger.info(f"   Success: {exec_result.success}")
    if exec_result.contract and exec_result.order:
        c = exec_result.contract
        o = exec_result.order
        logger.info(
            f"   Resolved Contract: {c.trading_symbol} (Token: {c.symbol_token}, Strike: {c.strike_price}, Lot: {c.lot_size})"
        )
        logger.info(
            f"   Paper Order Filled: {o.action} {o.lots} lot(s) ({o.quantity} qty) @ ₹{o.fill_price:.2f} (Charges: ₹{o.charges:.2f})"
        )

    # 3. Simulate Live Market Ticks
    symbol = exec_result.contract.trading_symbol
    logger.info(f"\n3. Simulating Live Market Ticks for {symbol}...")

    # Tick 1: Price goes to 205 (In profit, but below target 230)
    logger.info("   Tick: LTP ₹205.00...")
    tick_res1 = orchestrator.on_market_tick(symbol=symbol, ltp=205.0)
    logger.info(f"   Unrealized PnL: ₹{tick_res1.get('unrealized_pnl'):.2f}")

    # Tick 2: Price surges to 235 (Hits target ₹230)
    logger.info("   Tick: LTP ₹235.00 (Hits target!)...")
    tick_res2 = orchestrator.on_market_tick(symbol=symbol, ltp=235.0)
    logger.info(f"   Triggered Exit: {tick_res2.get('triggered_exit')}")

    # 4. Generate Validation Scorecard Report
    logger.info("\n4. Generating Paper Trading Validation Scorecard Report...")
    report = orchestrator.generate_validation_report()
    print("\n" + report + "\n")

    logger.info("=== SIMULATION COMPLETE: ZERO REAL ORDERS WERE PLACED ===")


if __name__ == "__main__":
    run_simulation()
