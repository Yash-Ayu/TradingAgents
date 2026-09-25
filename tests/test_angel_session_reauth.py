from tradingagents.runtime.market_adapter import AngelOneMarketAdapter


class FakeClient:
    def __init__(self):
        self.is_authenticated = True
        self.auth_calls = 0
        self.quote_calls = 0

    def authenticate(self):
        self.auth_calls += 1
        self.is_authenticated = True
        return {"status": True}

    def get_market_data(self, mode, exchange_tokens):
        self.quote_calls += 1

        if self.quote_calls == 1:
            return {
                "status": False,
                "message": "Invalid Token",
                "errorcode": "AG8001",
            }

        return {
            "status": True,
            "data": {
                "fetched": [{
                    "ltp": 980.50,
                    "tradeVolume": 100,
                }]
            },
        }


def test_expired_angel_session_reauthenticates_once_and_retries():
    client = FakeClient()
    adapter = AngelOneMarketAdapter(client=client)

    result = adapter.get_quote("SBIN")

    assert client.auth_calls == 1
    assert client.quote_calls == 2
    assert result["ltp"] == 980.50
    assert result["source"] == "LIVE_ANGEL_ONE"
