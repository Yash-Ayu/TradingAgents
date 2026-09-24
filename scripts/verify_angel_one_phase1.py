"""
Live Read-Only Verification Script for Angel One Phase 1.
Verifies credentials, authentication, Scrip Master download, dynamic contract resolution,
and ensures zero trade execution.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure tradingagents is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.integrations.angel_one.client import AngelOneClient, redact_secret
from tradingagents.integrations.angel_one.contract_resolver import FOContractResolver
from tradingagents.integrations.angel_one.models import (
    DerivativeType,
    MarketBias,
    ResolutionRequest,
    StrikeMode,
)
from tradingagents.integrations.angel_one.scrip_master import ScripMasterManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Phase1Verification")


def run_verification():
    load_dotenv()
    logger.info("=== PHASE 1: READ-ONLY LIVE VERIFICATION ===")

    # 1. Credential Presence Check (No secrets printed)
    logger.info("1. Checking credential presence in environment...")
    keys_to_check = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    all_present = True
    for k in keys_to_check:
        val = os.getenv(k)
        if val:
            logger.info(f"   {k}: PRESENT ({redact_secret(val)})")
        else:
            logger.warning(f"   {k}: MISSING")
            all_present = False

    # 2. Authentication Test
    logger.info("\n2. Testing Angel One Authentication (Read-Only)...")
    client = AngelOneClient()
    auth_res = client.authenticate()
    logger.info(f"   Auth Status: {auth_res.get('status')}")
    logger.info(f"   Auth Message: {auth_res.get('message')}")
    if auth_res.get("status"):
        logger.info(f"   Masked Client: {auth_res.get('client_code_masked')}")

        # Profile connectivity
        profile_res = client.get_profile()
        logger.info(f"   Profile Fetch Status: {profile_res.get('status')}")

        # RMS / Margin check
        rms_res = client.get_rms()
        logger.info(f"   RMS Fetch Status: {rms_res.get('status')}")

    # 3. Scrip Master Download & Indexing Test
    logger.info("\n3. Testing Scrip Master Manager...")
    scrip_mgr = ScripMasterManager()
    logger.info(f"   Cache dir: {scrip_mgr.cache_dir}")
    scrip_mgr.load(force_download=False)
    logger.info("   Scrip Master loaded successfully.")

    # 4. Dynamic Contract Resolution across Target Indices
    logger.info("\n4. Testing Dynamic Contract Resolution across Target Indices...")
    resolver = FOContractResolver(scrip_master=scrip_mgr)

    target_indices = [
        ("NIFTY", 25000.0),
        ("BANKNIFTY", 54000.0),
        ("SENSEX", 82000.0),
        ("FINNIFTY", 24000.0),
        ("MIDCPNIFTY", 13000.0),
        ("BANKEX", 60000.0),
    ]

    for underlying, spot in target_indices:
        req = ResolutionRequest(
            underlying=underlying,
            spot_price=spot,
            bias=MarketBias.BULLISH,
            strike_mode=StrikeMode.ATM,
        )
        res = resolver.resolve(req)
        if res.success and res.contract:
            c = res.contract
            logger.info(
                f"   [PASS] {underlying:<10} -> Symbol: {c.trading_symbol:<25} "
                f"Token: {c.symbol_token:<8} Exch: {c.exchange.value:<5} "
                f"Expiry: {c.expiry_date:<11} Strike: {c.strike_price:<8} Lot: {c.lot_size}"
            )
        else:
            logger.warning(f"   [FAIL] {underlying:<10} -> Reason: {res.error_reason}")

    # 5. Fail-Closed Verification
    logger.info("\n5. Verifying Fail-Closed Behavior...")
    # Unknown Symbol
    res_bad = resolver.resolve(
        ResolutionRequest(underlying="INVALID_SYM", spot_price=100.0, bias=MarketBias.BULLISH)
    )
    logger.info(f"   Unknown Symbol Rejected: {not res_bad.success} (Reason: {res_bad.error_reason})")

    # Neutral Bias
    res_neutral = resolver.resolve(
        ResolutionRequest(underlying="NIFTY", spot_price=25000.0, bias=MarketBias.NEUTRAL)
    )
    logger.info(f"   Neutral Bias Rejected: {not res_neutral.success} (Reason: {res_neutral.error_reason})")

    logger.info("\n=== VERIFICATION COMPLETE: NO REAL OR PAPER ORDERS WERE EXECUTED ===")


if __name__ == "__main__":
    run_verification()

