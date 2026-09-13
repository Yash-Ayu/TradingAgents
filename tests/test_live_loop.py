from tradingagents.runtime.live_loop import LiveTradingLoop


def test_live_loop_rejects_when_risky():
    loop = LiveTradingLoop()
    result = loop.run({
        "is_market_open": True,
        "vix": 42.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }, symbol="NIFTY")

    assert result["allowed"] is False
    assert result["action"] == "flatten"


def test_live_loop_accepts_safe_market():
    loop = LiveTradingLoop()
    result = loop.run({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    }, symbol="NIFTY")

    assert result["allowed"] is True
    assert result["action"] == "monitor"
