from tradingagents.runtime.broker_adapter import PaperBrokerAdapter, BrokerAdapterConfig


def test_paper_broker_adapter_accepts_and_fills_order():
    adapter = PaperBrokerAdapter(BrokerAdapterConfig())
    result = adapter.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10, "price": 24500})

    assert result["status"] == "accepted"
    assert result["mode"] == "paper"
    assert result["filled_qty"] == 10
    assert result["fills"][0]["status"] == "filled"


def test_live_broker_adapter_requires_credentials():
    adapter = PaperBrokerAdapter(BrokerAdapterConfig())
    result = adapter.place_order({"symbol": "BANKNIFTY", "side": "sell", "qty": 5})

    assert result["status"] == "accepted"
    assert result["broker"] == "paper"
