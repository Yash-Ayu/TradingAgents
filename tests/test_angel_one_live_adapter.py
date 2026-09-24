from tradingagents.runtime.broker_adapter import (
    AngelOneBrokerAdapter,
    BrokerAdapterConfig,
)


class FakeSmartConnect:
    last_order = None

    def __init__(self, api_key):
        self.api_key = api_key

    def generateSession(self, client_id, mpin, otp):
        return {"status": True, "data": {"jwtToken": "hidden"}}

    def placeOrder(self, order):
        type(self).last_order = order
        return "ANGEL-ORDER-1"


def valid_config(**overrides):
    values = {
        "broker_name": "angel",
        "api_key": "api-key",
        "client_id": "client-id",
        "mpin": "1234",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "live_trading_enabled": True,
        "live_confirmation": "I_UNDERSTAND_LIVE_TRADING",
    }
    values.update(overrides)
    return BrokerAdapterConfig(**values)


def valid_order(**overrides):
    order = {
        "symbol": "NIFTY",
        "side": "BUY",
        "qty": 1,
        "exchange": "NSE",
        "tradingsymbol": "NIFTY 50",
        "symboltoken": "99926000",
        "order_type": "MARKET",
        "risk_approved": True,
        "price": 23400,
    }
    order.update(overrides)
    return order


def test_live_order_requires_explicit_enable_and_confirmation():
    adapter = AngelOneBrokerAdapter(valid_config(live_trading_enabled=False))
    result = adapter.place_order(valid_order())

    assert result["status"] == "rejected"
    assert result["reason"] == "live_trading_disabled"


def test_live_order_calls_smartapi_only_after_safety_checks():
    adapter = AngelOneBrokerAdapter(valid_config(), client_factory=FakeSmartConnect)
    result = adapter.place_order(valid_order())

    assert result == {
        "status": "accepted",
        "broker": "angel",
        "mode": "live",
        "symbol": "NIFTY",
        "order_id": "ANGEL-ORDER-1",
        "fill_status": "unknown",
    }
    assert FakeSmartConnect.last_order["quantity"] == "1"
    assert FakeSmartConnect.last_order["producttype"] == "MIS"
