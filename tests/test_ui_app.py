from tradingagents.runtime.ui_app import TradingAppUI


def test_ui_app_renders_status_and_controls():
    app = TradingAppUI()
    status = app.get_status()

    assert "status" in status
    assert "market_open" in status
    assert "risk_state" in status
    assert "controls" in status
    assert "start" in status["controls"]
    assert "stop" in status["controls"]
