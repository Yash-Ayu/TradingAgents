from tradingagents.runtime.app_config import AppRuntimeConfig, launch_trading_app


def test_single_config_connects_selected_broker():
    config = AppRuntimeConfig(
        broker_name="zerodha",
        api_key="demo_key",
        api_secret="demo_secret",
        base_url="https://example.test",
        symbols=["NIFTY", "BANKNIFTY"],
    )

    result = config.connect()
    assert result["status"] == "not_implemented"
    assert result["broker"] == "zerodha"
    assert result["mode"] == "live"


def test_launch_trading_app_returns_runtime_ready_state():
    status = launch_trading_app(
        broker_name="paper",
        symbols=["NIFTY"],
    )

    assert status["status"] == "ready"
    assert status["broker"] == "paper"
    assert status["mode"] == "paper"
    assert status["symbols"] == ["NIFTY"]
