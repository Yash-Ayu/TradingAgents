from tradingagents.runtime.broker_adapter import BrokerAdapterConfig, create_broker_adapter


def test_create_broker_adapter_supports_all_requested_brokers():
    brokers = [
        ("paper", "paper"),
        ("angel", "angel"),
        ("angel_one", "angel"),
        ("zerodha", "zerodha"),
        ("upstox", "upstox"),
    ]

    for name, expected in brokers:
        adapter = create_broker_adapter(BrokerAdapterConfig(broker_name=name))
        assert adapter.broker_name == expected


def test_live_broker_adapter_accepts_configured_zerodha_and_upstox():
    zerodha = create_broker_adapter(BrokerAdapterConfig(
        broker_name="zerodha",
        api_key="key",
        api_secret="secret",
        base_url="https://example.com",
    ))
    upstox = create_broker_adapter(BrokerAdapterConfig(
        broker_name="upstox",
        api_key="key",
        api_secret="secret",
        base_url="https://example.com",
    ))

    assert zerodha.place_order({"symbol": "NIFTY", "side": "buy", "qty": 5})["status"] == "accepted"
    assert upstox.place_order({"symbol": "BANKNIFTY", "side": "sell", "qty": 3})["status"] == "accepted"


def test_live_broker_adapter_rejects_angel_without_credentials():
    adapter = create_broker_adapter(BrokerAdapterConfig(broker_name="angel"))
    result = adapter.place_order({"symbol": "NIFTY", "side": "buy", "qty": 1})

    assert result["status"] == "rejected"
    assert result["reason"] == "credentials_missing"
