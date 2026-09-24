from tradingagents.runtime.order_gateway import MarketOrderGateway


def test_market_order_gateway_rejects_brokerless_orders():
    gateway = MarketOrderGateway()
    result = gateway.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10})

    assert result["status"] == "rejected"
    assert result["reason"] == "broker_not_configured"


def test_market_order_gateway_rejects_missing_price():
    gateway = MarketOrderGateway(broker_name="paper")
    result = gateway.place_order({"symbol": "NIFTY", "side": "buy", "qty": 10})

    assert result["status"] == "rejected"
    assert result["reason"] == "invalid_paper_order"


def test_market_order_gateway_simulates_paper_fill():
    gateway = MarketOrderGateway(broker_name="paper")
    result = gateway.place_order({
        "symbol": "NIFTY",
        "side": "buy",
        "qty": 25,
        "price": 24500,
        "order_type": "market",
    })

    assert result["status"] == "accepted"
    assert result["mode"] == "paper"
    assert result["filled_qty"] == 25
    assert result["order_id"]
