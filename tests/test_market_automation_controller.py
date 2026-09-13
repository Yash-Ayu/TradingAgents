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


def test_market_automation_controller_routes_safe_paper_trade():
    controller = MarketAutomationController(broker_name="paper")

    result = controller.tick({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    }, symbol="NIFTY")

    assert result["status"] == "accepted"
    assert result["broker"] == "paper"
    assert result["mode"] == "paper"
    assert result["action"] == "monitor"
