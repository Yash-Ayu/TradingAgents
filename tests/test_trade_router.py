from tradingagents.runtime.trade_router import TradeRouter


def test_trade_router_rejects_without_broker_and_risk_clearance():
    router = TradeRouter()
    result = router.route({
        "is_market_open": True,
        "vix": 40.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }, symbol="NIFTY")

    assert result["status"] == "blocked"
    assert result["action"] == "flatten"


def test_trade_router_accepts_safe_brokered_trade():
    router = TradeRouter(broker_name="paper")
    result = router.route({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    }, symbol="NIFTY")

    assert result["status"] == "accepted"
    assert result["broker"] == "paper"
    assert result["mode"] == "paper"
    assert result["fills"][0]["status"] == "filled"
