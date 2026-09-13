from tradingagents.runtime.broker_adapter import LiveBrokerAdapter, BrokerAdapterConfig


def test_live_broker_adapter_requires_credentials():
    adapter = LiveBrokerAdapter(BrokerAdapterConfig(broker_name="zerodha"))
    result = adapter.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10, "price": 24500})

    assert result["status"] == "rejected"
    assert result["reason"] == "credentials_missing"


def test_live_broker_adapter_accepts_configured_live_order():
    adapter = LiveBrokerAdapter(BrokerAdapterConfig(
        broker_name="zerodha",
        api_key="demo_key",
        api_secret="demo_secret",
        base_url="https://example.test",
    ))
    result = adapter.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10, "price": 24500})

    assert result["status"] == "accepted"
    assert result["mode"] == "live"
    assert result["broker"] == "zerodha"
    assert result["filled_qty"] == 10
