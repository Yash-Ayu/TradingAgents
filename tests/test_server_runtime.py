from tradingagents.runtime import TradingRuntimeServer


def test_runtime_server_exposes_safe_endpoints():
    server = TradingRuntimeServer()
    started = server.start()
    status = server.health()

    assert started["status"] in {"running", "stopped"}
    assert status["status"] in {"running", "stopped"}
    assert "risk_state" in status
    assert "controls" in status

    decision = server.evaluate({
        "is_market_open": True,
        "vix": 40.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }, symbol="NIFTY")

    assert decision["allowed"] is False
    assert decision["action"] == "flatten"
