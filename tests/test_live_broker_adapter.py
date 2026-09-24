from tradingagents.runtime.broker_adapter import BrokerAdapterConfig, LiveBrokerAdapter


def test_live_broker_adapter_requires_credentials():
    adapter = LiveBrokerAdapter(BrokerAdapterConfig(broker_name="zerodha"))
    result = adapter.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10, "price": 24500})

    assert result["status"] == "rejected"
    assert result["reason"] == "credentials_missing"


def test_live_broker_adapter_rejects_unimplemented_live_order():
    adapter = LiveBrokerAdapter(BrokerAdapterConfig(
        broker_name="zerodha",
        api_key="demo_key",
        api_secret="demo_secret",
        base_url="https://example.test",
    ))
    result = adapter.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10, "price": 24500})

    assert result["status"] == "rejected"
    assert result["mode"] == "live"
    assert result["broker"] == "zerodha"
    assert result["reason"] == "live_order_not_implemented"
    assert "fills" not in result
