"""
Unit tests for Angel One Integration Phase 2: Deterministic Risk Engine.
Verifies hard mathematical rules, capital/lot constraints, market hours,
stop-loss requirements, circuit breakers, and emergency kill switch.
"""

from datetime import date, datetime, time, timedelta

import pytest

from tradingagents.integrations.angel_one.models import (
    ContractSpec,
    DerivativeType,
    Exchange,
    OptionType,
)
from tradingagents.integrations.angel_one.risk_engine import (
    DeterministicRiskEngine,
    RiskConfig,
    RiskEvaluationRequest,
)


@pytest.fixture
def sample_contract():
    """A valid NIFTY 25000 CE contract for testing."""
    return ContractSpec(
        underlying="NIFTY",
        exchange=Exchange.NFO,
        trading_symbol="NIFTY24OCT25000CE",
        symbol_token="57796",
        instrument_type=DerivativeType.OPTIDX,
        option_type=OptionType.CE,
        strike_price=25000.0,
        expiry_date="24OCT2024",
        lot_size=25,
        tick_size=0.05,
    )


@pytest.fixture
def risk_engine():
    """Standard risk engine instance with predefined test config."""
    config = RiskConfig(
        max_capital_per_trade=50000.0,
        max_lots_per_trade=2,
        max_open_positions=2,
        max_daily_trades=5,
        max_daily_loss=5000.0,
        max_loss_per_trade=2000.0,
        max_data_age_seconds=10.0,
        max_bid_ask_spread_pct=3.0,
        enforce_market_hours=True,
        enforce_stop_loss=True,
    )
    return DeterministicRiskEngine(config=config)


# A valid trading timestamp: Wednesday 10:30 AM IST
VALID_TRADING_TIME = datetime(2024, 10, 23, 10, 30, 0)


# =============================================================================
# 1. POSITIVE APPROVAL TEST
# =============================================================================
def test_valid_trade_approval(risk_engine, sample_contract):
    """A trade satisfying all rules must be approved."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,  # 1 lot * 25 * 200 = 5,000 INR (< 50,000 max)
        stop_loss=150.0,    # Risk = (200 - 150) * 25 = 1,250 INR (< 2,000 max)
        evaluation_time=VALID_TRADING_TIME,
        quote_timestamp=VALID_TRADING_TIME - timedelta(seconds=2),
        bid_price=199.0,
        ask_price=201.0,    # Spread ~ 1% (< 3%)
    )
    res = risk_engine.evaluate(req)
    assert res.approved is True
    assert res.allocated_lots == 1
    assert res.allocated_capital == 5000.0
    assert res.max_loss_risk == 1250.0
    assert res.violation_code is None


# =============================================================================
# 2. EMERGENCY KILL SWITCH TESTS
# =============================================================================
def test_emergency_kill_switch(risk_engine, sample_contract):
    """When kill switch is active, ALL trades must be rejected."""
    risk_engine.trigger_kill_switch("Manual emergency stop by owner")

    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "KILL_SWITCH_ACTIVE"

    # Unlock test
    risk_engine.unlock_kill_switch("Owner cleared risk state")
    res_unlocked = risk_engine.evaluate(req)
    assert res_unlocked.approved is True


# =============================================================================
# 3. CAPITAL & LOT LIMIT TESTS
# =============================================================================
def test_excessive_lots_rejected(risk_engine, sample_contract):
    """Proposing more lots than max_lots_per_trade must be rejected."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=3,  # Max is 2
        entry_price=100.0,
        stop_loss=80.0,
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "EXCEEDS_MAX_LOTS"


def test_excessive_capital_rejected(risk_engine, sample_contract):
    """Trade requiring more capital than max_capital_per_trade must be rejected."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=2,
        entry_price=1200.0,  # 2 * 25 * 1200 = 60,000 INR (> 50,000 max)
        stop_loss=1150.0,
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "EXCEEDS_MAX_CAPITAL"


# =============================================================================
# 4. STOP LOSS VALIDATION TESTS
# =============================================================================
def test_missing_stop_loss_rejected(risk_engine, sample_contract):
    """Missing stop loss must be rejected when enforce_stop_loss is True."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=None,
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "MISSING_STOP_LOSS"


def test_invalid_stop_loss_price(risk_engine, sample_contract):
    """For BUY, stop loss >= entry price is logically invalid and must be rejected."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=210.0,  # SL above entry
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "INVALID_STOP_LOSS"


def test_excessive_loss_risk_rejected(risk_engine, sample_contract):
    """Trade risk exceeding max_loss_per_trade must be rejected."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=2,    # 50 qty
        entry_price=200.0,
        stop_loss=150.0,    # Risk = 50 * 50 = 2,500 INR (> 2,000 max)
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "EXCEEDS_MAX_LOSS_PER_TRADE"


# =============================================================================
# 5. MARKET TIMING & CALENDAR TESTS
# =============================================================================
def test_weekend_trading_rejected(risk_engine, sample_contract):
    """Trades attempted on weekends must be rejected."""
    saturday_time = datetime(2024, 10, 26, 11, 0, 0)
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=saturday_time,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "MARKET_CLOSED_WEEKEND"


def test_pre_market_rejected(risk_engine, sample_contract):
    """Trades attempted before 09:15 IST must be rejected."""
    early_time = datetime(2024, 10, 23, 9, 5, 0)
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=early_time,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "MARKET_NOT_OPEN"


def test_after_cutoff_rejected(risk_engine, sample_contract):
    """Trades attempted after 15:15 IST must be rejected."""
    late_time = datetime(2024, 10, 23, 15, 20, 0)
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=late_time,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "AFTER_CUTOFF_TIME"


# =============================================================================
# 6. STALE DATA & SPREAD TESTS
# =============================================================================
def test_stale_data_rejected(risk_engine, sample_contract):
    """Quotes older than max_data_age_seconds must be rejected."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=VALID_TRADING_TIME,
        quote_timestamp=VALID_TRADING_TIME - timedelta(seconds=25),  # 25s old (> 10s max)
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "STALE_MARKET_DATA"


def test_wide_spread_rejected(risk_engine, sample_contract):
    """Wide bid-ask spread must be rejected."""
    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=VALID_TRADING_TIME,
        bid_price=180.0,
        ask_price=200.0,  # Spread = (200 - 180) / 200 = 10% (> 3% max)
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "WIDE_BID_ASK_SPREAD"


# =============================================================================
# 7. CONCURRENCY, DUPLICATES & CIRCUIT BREAKER TESTS
# =============================================================================
def test_duplicate_position_rejected(risk_engine, sample_contract):
    """Attempting a duplicate position for the same symbol must be rejected."""
    risk_engine.record_trade_opened(
        symbol=sample_contract.trading_symbol,
        lots=1,
        capital=5000.0,
        entry_price=200.0,
        stop_loss=150.0,
    )

    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "DUPLICATE_POSITION_EXISTS"


def test_max_positions_reached(risk_engine, sample_contract):
    """Reaching max_open_positions limit blocks new trades."""
    risk_engine.record_trade_opened("SYMBOL_1", 1, 5000.0, 100.0, 80.0)
    risk_engine.record_trade_opened("SYMBOL_2", 1, 5000.0, 100.0, 80.0)

    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "MAX_OPEN_POSITIONS_REACHED"


def test_daily_loss_circuit_breaker(risk_engine, sample_contract):
    """When cumulative losses reach max_daily_loss, kill switch auto-triggers."""
    risk_engine.record_trade_closed("SYMBOL_1", -3000.0)
    risk_engine.record_trade_closed("SYMBOL_2", -2500.0)  # Total loss: -5500 (> -5000 max)

    assert risk_engine.is_emergency_locked is True

    req = RiskEvaluationRequest(
        contract=sample_contract,
        action="BUY",
        proposed_lots=1,
        entry_price=200.0,
        stop_loss=150.0,
        evaluation_time=VALID_TRADING_TIME,
    )
    res = risk_engine.evaluate(req)
    assert res.approved is False
    assert res.violation_code == "KILL_SWITCH_ACTIVE"

