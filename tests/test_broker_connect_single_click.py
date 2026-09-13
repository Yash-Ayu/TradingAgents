from tradingagents.runtime.broker_adapter import connect_broker, BrokerAdapterConfig


def test_connect_broker_single_click_for_paper_and_live():
    paper = connect_broker("paper")
    live = connect_broker(
        "zerodha",
        api_key="demo_key",
        api_secret="demo_secret",
        base_url="https://example.test",
    )

    assert paper["status"] == "connected"
    assert paper["mode"] == "paper"
    assert live["status"] == "connected"
    assert live["broker"] == "zerodha"
    assert live["mode"] == "live"


def test_connect_broker_requires_credentials_for_live_mode():
    result = connect_broker("angel")

    assert result["status"] == "not_configured"
    assert result["broker"] == "angel"
