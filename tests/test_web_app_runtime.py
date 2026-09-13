from tradingagents.runtime.web_app import TradingWebApp


def test_web_app_exposes_health_and_controls():
    app = TradingWebApp()
    health = app.health()

    assert health["status"] in {"running", "stopped"}
    assert "risk_state" in health
    assert "market_open" in health
    assert "controls" in health
    assert "start" in health["controls"]
    assert "stop" in health["controls"]
