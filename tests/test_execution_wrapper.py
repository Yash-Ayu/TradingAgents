from tradingagents.runtime import SafeExecutionWrapper


def test_execution_wrapper_checks_gate_before_trade():
    wrapper = SafeExecutionWrapper()

    blocked = wrapper.run({
        "is_market_open": True,
        "vix": 40.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }, symbol="NIFTY")

    assert blocked["allowed"] is False
    assert blocked["action"] == "flatten"

    safe = wrapper.run({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    }, symbol="NIFTY")

    assert safe["allowed"] is True
    assert safe["action"] == "monitor"
