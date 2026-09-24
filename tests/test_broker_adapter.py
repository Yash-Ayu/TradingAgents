from tradingagents.runtime.broker_adapter import BrokerAdapterConfig, PaperBrokerAdapter


def test_paper_broker_adapter_accepts_and_fills_order():
    adapter = PaperBrokerAdapter(BrokerAdapterConfig())
    result = adapter.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10, "price": 24500})

    assert result["status"] == "accepted"
    assert result["mode"] == "paper"
    assert result["filled_qty"] == 10
    assert result["fills"][0]["status"] == "filled"


def test_paper_broker_rejects_missing_price():
    adapter = PaperBrokerAdapter(BrokerAdapterConfig())
    result = adapter.place_order({"symbol": "BANKNIFTY", "side": "sell", "qty": 5})

    assert result["status"] == "rejected"
    assert result["reason"] == "invalid_paper_order"
