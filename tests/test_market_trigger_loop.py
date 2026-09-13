from tradingagents.runtime.market_trigger import MarketTriggerLoop


def test_market_trigger_loop_rejects_risky_session():
    loop = MarketTriggerLoop()
    result = loop.evaluate({
        "is_market_open": True,
        "vix": 40.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }, symbol="NIFTY")

    assert result["allowed"] is False
    assert result["action"] == "flatten"


def test_market_trigger_loop_allows_safe_session():
    loop = MarketTriggerLoop()
    result = loop.evaluate({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    }, symbol="NIFTY")

    assert result["allowed"] is True
    assert result["action"] == "monitor"
