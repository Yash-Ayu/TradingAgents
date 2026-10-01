"""
Options Volatility and Greeks Engine for Indian F&O.

Provides:
1. Black-Scholes-Merton option pricing model for Call and Put options.
2. Complete Greeks calculation (Delta, Gamma, Theta, Vega, Rho).
3. Numerical Implied Volatility (IV) solver using hybrid Newton-Raphson / Bisection method.
4. Delta strike selection and filtering (e.g. 0.35 <= |Delta| <= 0.65).
5. 100% pure Python standard library (math module) with zero mandatory external binary dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Union


def norm_cdf(x: float) -> float:
    """Cumulative distribution function for standard normal distribution."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Probability density function for standard normal distribution."""
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)


@dataclass
class OptionGreeks:
    """Option pricing and Greeks summary."""
    theoretical_price: float
    delta: float
    gamma: float
    theta: float  # Per calendar day decay
    vega: float   # Per 1% IV change
    rho: float    # Per 1% interest rate change
    implied_volatility: float


def black_scholes_price(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float = 0.065,  # Indian 91-day T-Bill risk-free rate proxy (~6.5%)
    option_type: str = "CE",
    dividend_yield: float = 0.0,
) -> float:
    """
    Computes Black-Scholes-Merton option price.

    Args:
        spot: Underlying spot price (S > 0)
        strike: Strike price (K > 0)
        time_to_expiry: Time to expiration in years (T >= 0)
        volatility: Annualized volatility (sigma > 0)
        rate: Risk-free interest rate (r >= 0, e.g. 0.065 for 6.5%)
        option_type: "CE" (Call) or "PE" (Put)
        dividend_yield: Dividend yield (q >= 0)
    """
    if spot <= 0 or strike <= 0:
        return 0.0

    opt = option_type.upper()
    is_call = opt in ("CE", "CALL", "C")

    # Near expiry edge case
    if time_to_expiry <= 1e-7:
        if is_call:
            return max(0.0, spot - strike)
        else:
            return max(0.0, strike - spot)

    if volatility <= 1e-7:
        df = math.exp(-rate * time_to_expiry)
        div_df = math.exp(-dividend_yield * time_to_expiry)
        if is_call:
            return max(0.0, spot * div_df - strike * df)
        else:
            return max(0.0, strike * df - spot * div_df)

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend_yield + 0.5 * volatility * volatility) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t

    df = math.exp(-rate * time_to_expiry)
    div_df = math.exp(-dividend_yield * time_to_expiry)

    if is_call:
        price = spot * div_df * norm_cdf(d1) - strike * df * norm_cdf(d2)
    else:
        price = strike * df * norm_cdf(-d2) - spot * div_df * norm_cdf(-d1)

    return max(0.0, price)


def calculate_greeks(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float = 0.065,
    option_type: str = "CE",
    dividend_yield: float = 0.0,
) -> OptionGreeks:
    """
    Computes all standard Greeks for an option.
    """
    opt = option_type.upper()
    is_call = opt in ("CE", "CALL", "C")

    if spot <= 0 or strike <= 0 or time_to_expiry <= 1e-7 or volatility <= 1e-7:
        price = black_scholes_price(spot, strike, time_to_expiry, volatility, rate, option_type, dividend_yield)
        delta = 1.0 if (is_call and spot > strike) else (-1.0 if (not is_call and strike > spot) else 0.0)
        return OptionGreeks(
            theoretical_price=price,
            delta=delta,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            rho=0.0,
            implied_volatility=volatility,
        )

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend_yield + 0.5 * volatility * volatility) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t

    df = math.exp(-rate * time_to_expiry)
    div_df = math.exp(-dividend_yield * time_to_expiry)
    pdf_d1 = norm_pdf(d1)

    # 1. Delta
    if is_call:
        delta = div_df * norm_cdf(d1)
    else:
        delta = -div_df * norm_cdf(-d1)

    # 2. Gamma (same for Call and Put)
    gamma = (div_df * pdf_d1) / (spot * volatility * sqrt_t)

    # 3. Vega (change per 1% move in volatility, i.e. / 100)
    vega_total = spot * div_df * sqrt_t * pdf_d1
    vega = vega_total / 100.0

    # 4. Theta (decay per calendar day, i.e. / 365)
    term1 = -(spot * volatility * div_df * pdf_d1) / (2.0 * sqrt_t)
    if is_call:
        term2 = -rate * strike * df * norm_cdf(d2)
        term3 = dividend_yield * spot * div_df * norm_cdf(d1)
        theta_annual = term1 + term2 + term3
    else:
        term2 = rate * strike * df * norm_cdf(-d2)
        term3 = -dividend_yield * spot * div_df * norm_cdf(-d1)
        theta_annual = term1 + term2 + term3
    theta = theta_annual / 365.0

    # 5. Rho (change per 1% move in interest rate, i.e. / 100)
    if is_call:
        rho_annual = strike * time_to_expiry * df * norm_cdf(d2)
    else:
        rho_annual = -strike * time_to_expiry * df * norm_cdf(-d2)
    rho = rho_annual / 100.0

    # Theoretical price
    if is_call:
        price = spot * div_df * norm_cdf(d1) - strike * df * norm_cdf(d2)
    else:
        price = strike * df * norm_cdf(-d2) - spot * div_df * norm_cdf(-d1)

    return OptionGreeks(
        theoretical_price=max(0.0, price),
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
        implied_volatility=volatility,
    )


def implied_volatility(
    market_price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float = 0.065,
    option_type: str = "CE",
    dividend_yield: float = 0.0,
    max_iterations: int = 50,
    tolerance: float = 1e-4,
) -> float:
    """
    Computes Black-Scholes Implied Volatility (IV) using a hybrid Newton-Raphson
    and Bisection solver.

    Returns annualized IV as a decimal (e.g., 0.18 for 18%).
    """
    if market_price <= 0 or spot <= 0 or strike <= 0 or time_to_expiry <= 1e-6:
        return 0.0

    opt = option_type.upper()
    is_call = opt in ("CE", "CALL", "C")

    # Lower bound / intrinsic value check
    df = math.exp(-rate * time_to_expiry)
    div_df = math.exp(-dividend_yield * time_to_expiry)
    intrinsic = max(0.0, (spot * div_df - strike * df) if is_call else (strike * df - spot * div_df))

    if market_price < intrinsic:
        return 0.001

    # Bounds for volatility search
    low_vol = 0.001
    high_vol = 5.0  # 500% max volatility

    # Initial guess using Brenner-Subrahmanyam approximation if ATM
    vol = math.sqrt(2.0 * math.pi / time_to_expiry) * (market_price / spot)
    vol = max(0.05, min(1.0, vol))

    # Newton-Raphson iteration with bisection fallback
    for _ in range(max_iterations):
        p = black_scholes_price(spot, strike, time_to_expiry, vol, rate, option_type, dividend_yield)
        diff = p - market_price

        if abs(diff) < tolerance:
            return round(vol, 4)

        # Update bisection bracket
        if diff > 0:
            high_vol = vol
        else:
            low_vol = vol

        # Vega calculation for derivative step
        sqrt_t = math.sqrt(time_to_expiry)
        d1 = (
            math.log(spot / strike)
            + (rate - dividend_yield + 0.5 * vol * vol) * time_to_expiry
        ) / (vol * sqrt_t)
        vega = spot * div_df * sqrt_t * norm_pdf(d1)

        # If vega is sufficient, use Newton-Raphson
        if vega > 1e-5:
            step = diff / vega
            new_vol = vol - step
            if low_vol <= new_vol <= high_vol:
                vol = new_vol
                continue

        # Bisection step if Newton-Raphson overshoots or fails
        vol = 0.5 * (low_vol + high_vol)

    return round(vol, 4)


def time_to_expiry_years(expiry_date: Union[date, datetime], current_date: Optional[Union[date, datetime]] = None) -> float:
    """
    Computes time to expiration in fraction of year (ACT/365).
    """
    if current_date is None:
        current_date = datetime.now()

    if isinstance(expiry_date, datetime):
        exp_dt = expiry_date
    else:
        exp_dt = datetime.combine(expiry_date, datetime.min.time().replace(hour=15, minute=30))

    if isinstance(current_date, datetime):
        cur_dt = current_date
    else:
        cur_dt = datetime.combine(current_date, datetime.min.time().replace(hour=9, minute=15))

    diff_seconds = (exp_dt - cur_dt).total_seconds()
    if diff_seconds <= 0:
        return 1e-6

    return diff_seconds / (365.0 * 86400.0)


def filter_strikes_by_delta(
    option_contracts: List[Dict[str, Any]],
    spot_price: float,
    time_to_expiry: float,
    rate: float = 0.065,
    min_delta: float = 0.35,
    max_delta: float = 0.65,
) -> List[Dict[str, Any]]:
    """
    Filters a list of option contracts to select strikes whose absolute delta
    falls within the optimal trading window [min_delta, max_delta].
    """
    eligible = []
    for c in option_contracts:
        strike = float(c.get("strike", 0))
        opt_type = c.get("option_type", "CE")
        ltp = float(c.get("ltp", 0))

        if strike <= 0:
            continue

        iv = 0.18
        if ltp > 0:
            try:
                calc_iv = implied_volatility(ltp, spot_price, strike, time_to_expiry, rate, opt_type)
                if calc_iv > 0.01:
                    iv = calc_iv
            except Exception:
                pass

        greeks = calculate_greeks(spot_price, strike, time_to_expiry, iv, rate, opt_type)
        abs_delta = abs(greeks.delta)

        if min_delta <= abs_delta <= max_delta:
            enriched = dict(c)
            enriched["greeks"] = {
                "delta": round(greeks.delta, 4),
                "gamma": round(greeks.gamma, 6),
                "theta": round(greeks.theta, 2),
                "vega": round(greeks.vega, 2),
                "rho": round(greeks.rho, 4),
                "iv": round(iv, 4),
                "theoretical_price": round(greeks.theoretical_price, 2),
            }
            eligible.append(enriched)

    return eligible
