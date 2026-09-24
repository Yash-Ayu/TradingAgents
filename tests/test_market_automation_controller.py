from tradingagents.runtime.controller import MarketAutomationController


def test_market_automation_controller_blocks_risky_snapshot():
    controller = MarketAutomationController()

    result = controller.tick({
        "is_market_open": True,
        "vix": 40.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }, symbol="NIFTY")

    assert result["status"] == "risk_blocked"
    assert result["action"] == "flatten"


def test_market_automation_controller_blocks_without_engine_decision():
    controller = MarketAutomationController(broker_name="paper")

    result = controller.tick({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    }, symbol="NIFTY")

    assert result["status"] == "blocked"
    assert result["reason"] == "no_actionable_engine_decision"
    assert result["action"] == "hold"
