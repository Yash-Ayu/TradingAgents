from tradingagents.runtime import LocalTradingDashboard


def test_dashboard_trigger_and_status():
    dashboard = LocalTradingDashboard()
    started = dashboard.start()
    status = dashboard.get_status()

    assert started == "running"
    assert status["status"] == "running"
    assert "market_open" in status
    assert "risk_state" in status

    decision = dashboard.run_cycle({
        "is_market_open": True,
        "vix": 40.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    }, symbol="NIFTY")

    assert decision["allowed"] is False
    assert decision["action"] == "flatten"

    stopped = dashboard.stop()
    assert stopped == "stopped"
