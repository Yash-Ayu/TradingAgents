"""
Unit tests for Options Greeks & Volatility Engine.
Validates Black-Scholes pricing, Greeks calculations, numerical IV solver,
and strike filtering by delta.
"""

import math

from tradingagents.integrations.angel_one.greeks import (
    black_scholes_price,
    calculate_greeks,
    filter_strikes_by_delta,
    implied_volatility,
)


def test_black_scholes_at_the_money_call_and_put():
    """Verify standard ATM call and put pricing."""
    spot = 24000.0
    strike = 24000.0
    time_to_expiry = 30.0 / 365.0  # ~30 days
    volatility = 0.15              # 15% IV
    rate = 0.065                   # 6.5% risk-free rate

    call_price = black_scholes_price(spot, strike, time_to_expiry, volatility, rate, "CE")
    put_price = black_scholes_price(spot, strike, time_to_expiry, volatility, rate, "PE")

    assert call_price > 0.0
    assert put_price > 0.0

    # Put-Call Parity: C - P = S - K * exp(-r * T)
    parity_diff = (call_price - put_price) - (spot - strike * math.exp(-rate * time_to_expiry))
    assert abs(parity_diff) < 0.01


def test_black_scholes_boundary_conditions():
    """Verify deep ITM, deep OTM, and expiration edge cases."""
    rate = 0.065
    vol = 0.18

    # Deep ITM Call (S=25000, K=20000)
    itm_call = black_scholes_price(25000.0, 20000.0, 0.1, vol, rate, "CE")
    assert itm_call > 4900.0

    # Deep OTM Call (S=20000, K=25000)
    otm_call = black_scholes_price(20000.0, 25000.0, 0.01, vol, rate, "CE")
    assert otm_call < 1.0

    # Immediate Expiration (T=0)
    exp_call = black_scholes_price(24100.0, 24000.0, 0.0, vol, rate, "CE")
    assert round(exp_call, 2) == 100.0

    exp_put = black_scholes_price(23900.0, 24000.0, 0.0, vol, rate, "PE")
    assert round(exp_put, 2) == 100.0

    exp_otm_call = black_scholes_price(23900.0, 24000.0, 0.0, vol, rate, "CE")
    assert exp_otm_call == 0.0


def test_greeks_properties():
    """Verify standard properties of option Greeks."""
    spot = 24500.0
    strike = 24500.0
    time_to_expiry = 20.0 / 365.0
    volatility = 0.16
    rate = 0.065

    call_greeks = calculate_greeks(spot, strike, time_to_expiry, volatility, rate, "CE")
    put_greeks = calculate_greeks(spot, strike, time_to_expiry, volatility, rate, "PE")

    # Delta bounds
    assert 0.45 <= call_greeks.delta <= 0.60
    assert -0.55 <= put_greeks.delta <= -0.40
    # Delta relationship: Call Delta - Put Delta ~ 1
    assert abs((call_greeks.delta - put_greeks.delta) - 1.0) < 0.02

    # Gamma must be positive and identical for Call and Put
    assert call_greeks.gamma > 0
    assert put_greeks.gamma > 0
    assert abs(call_greeks.gamma - put_greeks.gamma) < 1e-6

    # Theta must be negative (time decay)
    assert call_greeks.theta < 0
    assert put_greeks.theta < 0

    # Vega must be positive and identical for Call and Put
    assert call_greeks.vega > 0
    assert put_greeks.vega > 0
    assert abs(call_greeks.vega - put_greeks.vega) < 1e-4

    # Rho: positive for Call, negative for Put
    assert call_greeks.rho > 0
    assert put_greeks.rho < 0


def test_implied_volatility_solver():
    """Verify numerical IV solver recovers true volatility from theoretical prices."""
    spot = 24200.0
    strike = 24200.0
    time_to_expiry = 15.0 / 365.0
    rate = 0.065
    target_iv = 0.215  # 21.5%

    # Calculate call market price at target IV
    market_call = black_scholes_price(spot, strike, time_to_expiry, target_iv, rate, "CE")

    # Recover IV using solver
    recovered_iv = implied_volatility(market_call, spot, strike, time_to_expiry, rate, "CE")
    assert abs(recovered_iv - target_iv) < 0.005

    # Calculate put market price at target IV
    market_put = black_scholes_price(spot, strike, time_to_expiry, target_iv, rate, "PE")
    recovered_put_iv = implied_volatility(market_put, spot, strike, time_to_expiry, rate, "PE")
    assert abs(recovered_put_iv - target_iv) < 0.005


def test_filter_strikes_by_delta():
    """Verify selection of option strikes strictly within 0.35 <= |Delta| <= 0.65."""
    spot = 24000.0
    time_to_expiry = 10.0 / 365.0
    rate = 0.065

    chain = [
        {"strike": 22000, "option_type": "CE", "ltp": 2010.0},  # Deep ITM -> Delta ~ 1.0 (rejected)
        {"strike": 23900, "option_type": "CE", "ltp": 250.0},   # Near ATM ITM -> Delta ~ 0.63 (accepted)
        {"strike": 24000, "option_type": "CE", "ltp": 190.0},   # ATM -> Delta ~ 0.54 (accepted)
        {"strike": 24200, "option_type": "CE", "ltp": 95.0},    # Near ATM OTM -> Delta ~ 0.35 (accepted)
        {"strike": 26000, "option_type": "CE", "ltp": 2.0},     # Deep OTM -> Delta ~ 0.01 (rejected)
    ]

    filtered = filter_strikes_by_delta(chain, spot, time_to_expiry, rate, min_delta=0.35, max_delta=0.65)
    selected_strikes = [c["strike"] for c in filtered]

    assert 22000 not in selected_strikes  # Rejected (too deep ITM)
    assert 26000 not in selected_strikes  # Rejected (too deep OTM)
    assert 23900 in selected_strikes
    assert 24000 in selected_strikes
    assert 24200 in selected_strikes

    for c in filtered:
        assert "greeks" in c
        assert 0.35 <= abs(c["greeks"]["delta"]) <= 0.65
