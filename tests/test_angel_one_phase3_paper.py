"""
Unit tests for Angel One Integration Phase 3: Indian F&O Paper Trading Engine.
Verifies virtual execution, slippage, statutory charges (STT, GST, SEBI, stamp duty),
position tracking, stop-loss/target auto-triggers, idempotency, and SQLite persistence.
"""

from pathlib import Path

import pytest

from tradingagents.integrations.angel_one.models import (
    ContractSpec,
    DerivativeType,
    Exchange,
    OptionType,
)
from tradingagents.integrations.angel_one.paper_engine import (
    FOPaperTradingEngine,
    IndianFOChargesCalculator,
)


@pytest.fixture
def sample_nifty_contract():
    """A valid NIFTY 25000 CE contract spec."""
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


# =============================================================================
# 1. CHARGES CALCULATOR TESTS
# =============================================================================
def test_charges_calculation_buy_vs_sell():
    """Verify Indian statutory charges for options BUY and SELL."""
    # BUY: 1 lot (25 qty) @ ₹200 -> turnover = ₹5,000
    buy_charges = IndianFOChargesCalculator.calculate(
        action="BUY",
        instrument_type=DerivativeType.OPTIDX,
        price=200.0,
        quantity=25,
    )
    assert buy_charges["turnover"] == 5000.0
    assert buy_charges["brokerage"] == 20.0
    assert buy_charges["stt"] == 0.0  # No STT on option buy
    assert buy_charges["stamp_duty"] > 0.0  # Stamp duty applies on buy
    assert buy_charges["gst"] > 0.0
    assert buy_charges["total_charges"] > 20.0

    # SELL: 1 lot (25 qty) @ ₹200 -> turnover = ₹5,000
    sell_charges = IndianFOChargesCalculator.calculate(
        action="SELL",
        instrument_type=DerivativeType.OPTIDX,
        price=200.0,
        quantity=25,
    )
    assert sell_charges["turnover"] == 5000.0
    assert sell_charges["brokerage"] == 20.0
    assert sell_charges["stt"] == 5.0  # 0.1% of 5,000 = ₹5
    assert sell_charges["stamp_duty"] == 0.0  # No stamp duty on sell


# =============================================================================
# 2. ORDER EXECUTION & SLIPPAGE TESTS
# =============================================================================
def test_paper_order_execution_and_slippage(sample_nifty_contract):
    """Verify order fill with realistic slippage."""
    engine = FOPaperTradingEngine(initial_capital=100000.0)

    # Submit BUY order with 0.5% slippage
    order = engine.submit_order(
        contract=sample_nifty_contract,
        action="BUY",
        lots=1,
        current_price=200.0,
        stop_loss=150.0,
        target=250.0,
        idempotency_key="TEST_ORD_001",
        slippage_pct=0.5,
    )

    assert order.status == "FILLED"
    assert order.order_price == 200.0
    assert order.fill_price == 201.0  # 200 + 0.5% (1.0)
    assert order.slippage == 1.0
    assert order.charges > 0.0

    # Open position check
    positions = engine.get_open_positions()
    assert len(positions) == 1
    assert positions[0].symbol == sample_nifty_contract.trading_symbol
    assert positions[0].entry_price == 201.0
    assert positions[0].quantity == 25


# =============================================================================
# 3. IDEMPOTENCY & DUPLICATE PROTECTION
# =============================================================================
def test_duplicate_order_idempotency(sample_nifty_contract):
    """Submitting with the same idempotency key must reject the duplicate order."""
    engine = FOPaperTradingEngine(initial_capital=100000.0)

    # First attempt
    order1 = engine.submit_order(
        contract=sample_nifty_contract,
        action="BUY",
        lots=1,
        current_price=200.0,
        idempotency_key="UNIQUE_KEY_123",
    )
    assert order1.status == "FILLED"

    # Second attempt with same key
    order2 = engine.submit_order(
        contract=sample_nifty_contract,
        action="BUY",
        lots=1,
        current_price=200.0,
        idempotency_key="UNIQUE_KEY_123",
    )
    assert order2.status == "REJECTED"
    assert order2.reason == "DUPLICATE_ORDER_IDEMPOTENT_BLOCK"


# =============================================================================
# 4. MARK-TO-MARKET & AUTOMATED EXIT TRIGGERS
# =============================================================================
def test_mark_price_and_stop_loss_trigger(sample_nifty_contract):
    """Updating LTP below stop-loss must auto-trigger position closure."""
    engine = FOPaperTradingEngine(initial_capital=100000.0)

    engine.submit_order(
        contract=sample_nifty_contract,
        action="BUY",
        lots=1,
        current_price=200.0,
        stop_loss=170.0,
        target=250.0,
        idempotency_key="ORDER_SL_TEST",
    )

    # Mark price drops to 180 (above SL) -> position stays open
    res = engine.update_mark_price(sample_nifty_contract.trading_symbol, 180.0)
    assert res["triggered_exit"] is None
    assert len(engine.get_open_positions()) == 1

    # Mark price drops to 169 (below SL 170) -> triggers SL exit
    res_sl = engine.update_mark_price(sample_nifty_contract.trading_symbol, 169.0)
    assert res_sl["triggered_exit"] == "STOP_LOSS_HIT"
    assert len(engine.get_open_positions()) == 0  # Position closed


def test_target_trigger(sample_nifty_contract):
    """Updating LTP above target must auto-trigger position closure."""
    engine = FOPaperTradingEngine(initial_capital=100000.0)

    engine.submit_order(
        contract=sample_nifty_contract,
        action="BUY",
        lots=1,
        current_price=200.0,
        stop_loss=170.0,
        target=240.0,
        idempotency_key="ORDER_TARGET_TEST",
    )

    # Mark price rises to 245 (above target 240) -> triggers Target exit
    res_tgt = engine.update_mark_price(sample_nifty_contract.trading_symbol, 245.0)
    assert res_tgt["triggered_exit"] == "TARGET_HIT"
    assert len(engine.get_open_positions()) == 0


# =============================================================================
# 5. PERFORMANCE METRICS & SCORECARD
# =============================================================================
def test_performance_scorecard(sample_nifty_contract):
    """Verify performance metrics calculation (win rate, profit factor, drawdown)."""
    engine = FOPaperTradingEngine(initial_capital=100000.0)

    # Trade 1: Profitable trade
    engine.submit_order(sample_nifty_contract, "BUY", 1, 200.0, idempotency_key="T1")
    engine.close_position(sample_nifty_contract.trading_symbol, exit_price=240.0, reason="PROFIT")

    # Trade 2: Losing trade
    engine.submit_order(sample_nifty_contract, "BUY", 1, 200.0, idempotency_key="T2")
    engine.close_position(sample_nifty_contract.trading_symbol, exit_price=180.0, reason="LOSS")

    stats = engine.get_performance_metrics()
    assert stats.total_trades == 2
    assert stats.winning_trades == 1
    assert stats.losing_trades == 1
    assert stats.win_rate_pct == 50.0
    assert stats.gross_profit > 0.0
    assert stats.gross_loss > 0.0
    assert stats.profit_factor > 0.0
    assert stats.total_charges_paid > 0.0


# =============================================================================
# 6. SQLITE PERSISTENCE ACROSS RESTARTS
# =============================================================================
def test_sqlite_persistence_across_restart(tmp_path, sample_nifty_contract):
    """Verify that ledger state survives engine restarts."""
    db_file = tmp_path / "paper_ledger.db"

    # Session 1: Open a position
    engine1 = FOPaperTradingEngine(db_path=db_file, initial_capital=100000.0)
    engine1.submit_order(sample_nifty_contract, "BUY", 1, 200.0, idempotency_key="PERSIST_1")
    assert len(engine1.get_open_positions()) == 1

    # Session 2: Reopen from same DB file (simulating restart)
    engine2 = FOPaperTradingEngine(db_path=db_file, initial_capital=100000.0)
    positions = engine2.get_open_positions()
    assert len(positions) == 1
    assert positions[0].symbol == sample_nifty_contract.trading_symbol
    assert positions[0].entry_price == 201.0  # with 0.5% slippage

