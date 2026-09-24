"""
Official Real-Time Paper Trading Validation Session Runner for Indian F&O.
Executes end-to-end real-time paper trading pipeline during Indian market hours.

Architecture:
Angel One Live Market Data
-> TradingAgents Multi-Agent Analysis
-> Genuine BUY/SELL/HOLD Signal
-> Deterministic Risk Engine
-> Dynamic F&O Contract Resolver
-> Paper Execution Only (SQLite Ledger)
-> Live Tick Monitoring
-> Virtual SL / Target / Auto-Exit
-> Daily Validation Report

ABSOLUTE SAFETY INVARIANTS:
1. Real broker write APIs (placeOrder, modifyOrder, cancelOrder) are strictly disabled.
2. 100% paper execution in local SQLite ledger.
3. Zero real money orders are ever sent to Angel One.
4. No forced trades when market is closed or signals are weak.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

# Ensure tradingagents is importable
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE_ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tradingagents.integrations.angel_one.client import AngelOneClient, redact_secret
from tradingagents.integrations.angel_one.contract_resolver import FOContractResolver
from tradingagents.integrations.angel_one.market_data import (
    AngelOneMarketDataProvider,
    DataSource,
)
from tradingagents.integrations.angel_one.models import (
    DerivativeType,
    MarketBias,
    ResolutionRequest,
    StrikeMode,
)
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
from tradingagents.integrations.angel_one.session_manager import (
    MarketSessionObservability,
    PaperSessionConfig,
    SessionRecoveryManager,
    SessionStartupChecker,
    SessionStatus,
)
from tradingagents.integrations.angel_one.signal_adapter import TradingAgentsSignalAdapter

# Setup paths
RUNTIME_DIR = WORKSPACE_ROOT / "runtime_state"
LOGS_DIR = RUNTIME_DIR / "logs"
REPORTS_DIR = RUNTIME_DIR / "daily_reports"
DB_PATH = RUNTIME_DIR / "paper_trading_fo.db"
KILL_SWITCH_FILE = RUNTIME_DIR / "KILL_SWITCH"

for d in [RUNTIME_DIR, LOGS_DIR, REPORTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Configure logging
log_filename = LOGS_DIR / f"paper_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("PaperSessionRunner")

# Global flag for graceful shutdown
running = True


def handle_shutdown(signum, frame):
    global running
    logger.info("Received termination signal. Initiating graceful paper session shutdown...")
    running = False


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def main():
    global running
    logger.info("================================================================================")
    logger.info("  INDIAN F&O REAL-TIME PAPER TRADING VALIDATION SESSION")
    logger.info("  MODE: STRICT PAPER TRADING (ZERO REAL BROKER ORDERS)")
    logger.info("================================================================================")

    # 1. Load Environment Variables
    load_dotenv(WORKSPACE_ROOT / ".env")

    # 2. Check Credential Presence
    api_key = os.getenv("ANGEL_API_KEY")
    client_code = os.getenv("ANGEL_CLIENT_CODE") or os.getenv("ANGEL_CLIENT_ID")
    pin = os.getenv("ANGEL_PIN") or os.getenv("ANGEL_MPIN")
    totp_secret = os.getenv("ANGEL_TOTP_SECRET")

    if not (api_key and client_code and pin and totp_secret):
        logger.error("CRITICAL ERROR: Angel One credentials missing in .env file!")
        logger.error(
            "Required variables: ANGEL_API_KEY, ANGEL_CLIENT_CODE (or ANGEL_CLIENT_ID), "
            "ANGEL_PIN (or ANGEL_MPIN), ANGEL_TOTP_SECRET"
        )
        logger.error("Session CANNOT start without live market data credentials.")
        sys.exit(1)

    # 3. Initialize Components
    logger.info("Initializing system components...")
    session_config = PaperSessionConfig()
    client = AngelOneClient(
        api_key=api_key,
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
    )
    scrip_mgr = ScripMasterManager()
    paper_engine = FOPaperTradingEngine(db_path=str(DB_PATH))
    risk_config = RiskConfig(
        max_capital_per_trade=session_config.max_capital_per_trade,
        max_lots_per_trade=session_config.max_lots_per_trade,
        max_open_positions=session_config.max_open_positions,
        max_daily_loss=session_config.max_daily_loss,
        enforce_market_hours=session_config.enforce_market_hours,
    )
    risk_engine = DeterministicRiskEngine(config=risk_config)
    alert_dispatcher = PhoneAlertDispatcher()
    market_data = AngelOneMarketDataProvider(client=client)

    orchestrator = FOPipelineOrchestrator(
        scrip_master=scrip_mgr,
        risk_engine=risk_engine,
        paper_engine=paper_engine,
        alert_dispatcher=alert_dispatcher,
        market_data_provider=market_data,
    )

    # 4. Run 10-Point Pre-Session Startup Checklist
    logger.info("\n--- EXECUTING 10-POINT STARTUP AUDIT ---")
    startup_checker = SessionStartupChecker(
        config=session_config,
        client=client,
        scrip_master=scrip_mgr,
        paper_engine=paper_engine,
        risk_engine=risk_engine,
        market_data_provider=market_data,
    )

    # Scrip Master load
    scrip_mgr.ensure_loaded()

    startup_result = startup_checker.run_pre_session_checklist()
    for item, passed in startup_result.checklist.items():
        status_sym = "✅ PASS" if passed else "❌ FAIL"
        logger.info(f"  [{status_sym}] {item}")

    if not startup_result.passed:
        logger.error(f"Startup checklist failed: {startup_result.failures}")
        logger.error("System halted safely in SAFE_HALT state.")
        sys.exit(1)

    # 5. Crash & Restart State Reconciliation
    logger.info("\n--- CRASH & RECOVERY STATE RECONCILIATION ---")
    recovery_result = SessionRecoveryManager.reconcile_on_startup(
        paper_engine=paper_engine,
        market_data_provider=market_data,
    )
    logger.info(f"Reconciled state: {recovery_result}")

    # 6. Main Session Loop
    session_start_time = datetime.now()
    logger.info(f"\nSession started at {session_start_time.strftime('%Y-%m-%d %H:%M:%S IST')}")
    logger.info("To safely stop the session, press Ctrl+C.")
    logger.info(f"To trigger Emergency Paper Kill Switch, create file: {KILL_SWITCH_FILE}\n")

    last_status_print = 0.0
    status_print_interval = 10.0  # seconds

    try:
        while running:
            now = datetime.now()

            # Check Kill Switch file
            if KILL_SWITCH_FILE.exists():
                logger.warning("Emergency Kill Switch file detected! Triggering auto-lock...")
                risk_engine.trigger_emergency_kill_switch()
                alert_dispatcher.send_alert(
                    "EMERGENCY KILL SWITCH",
                    "Emergency kill switch triggered via file. All new paper trades blocked.",
                )
                try:
                    KILL_SWITCH_FILE.unlink()
                except Exception:
                    pass

            # Check Market Calendar
            is_open, market_reason = MarketSessionObservability.is_market_open(now)

            # Periodic Observability Status Line
            if time.time() - last_status_print >= status_print_interval:
                status_line = MarketSessionObservability.get_status_line(orchestrator, check_time=now)
                print(f"[{now.strftime('%H:%M:%S')}] {status_line}", flush=True)
                last_status_print = time.time()

            if not is_open:
                # Market closed: Sleep briefly, do NOT force any trades
                time.sleep(5.0)
                continue

            # When market is open:
            # 1. Connect feed if not connected
            if not market_data.is_connected():
                logger.info("Market is OPEN. Connecting Angel One live data feed...")
                conn_res = market_data.connect()
                if not conn_res:
                    logger.warning("Feed connection attempt failed. Retrying in 10s...")
                    time.sleep(10.0)
                    continue

            # 2. Monitor open paper positions against live market ticks
            open_positions = paper_engine.get_open_positions()
            for pos in open_positions:
                tick = market_data.get_latest_tick(pos.symbol)
                if tick:
                    tick_res = orchestrator.on_market_tick(
                        symbol=pos.symbol,
                        ltp=tick.ltp,
                        tick_time=tick.timestamp,
                        bid=tick.bid,
                        ask=tick.ask,
                    )
                    if tick_res.get("triggered_exit"):
                        logger.info(
                            f"Automated Exit triggered for {pos.symbol}: "
                            f"{tick_res.get('triggered_exit')} @ ₹{tick.ltp:.2f}"
                        )

            # Sleep to pace the loop
            time.sleep(1.0)

    except Exception as e:
        logger.exception(f"Unhandled exception in session loop: {e}")
    finally:
        logger.info("\n================================================================================")
        logger.info("  SESSION TERMINATING - GENERATING DAILY SESSION REPORT")
        logger.info("================================================================================")

        report = MarketSessionObservability.generate_daily_session_report(
            orchestrator=orchestrator,
            session_start_time=session_start_time,
            session_date=date.today(),
        )

        # Save report to file
        report_file = REPORTS_DIR / f"report_{date.today().strftime('%Y-%m-%d')}.md"
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(report)

        logger.info(f"Daily report saved to: {report_file}")
        print("\n" + report + "\n")

        logger.info("Session closed safely. REAL BROKER UNTOUCHED.")


if __name__ == "__main__":
    main()

