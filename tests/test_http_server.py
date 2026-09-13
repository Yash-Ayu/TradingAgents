from tradingagents.runtime.http_server import TradingHTTPServer


def test_http_server_exposes_status_and_actions():
    server = TradingHTTPServer()
    health = server.health()

    assert "status" in health
    assert "risk_state" in health
    assert "controls" in health
    assert "start" in health["controls"]
    assert "stop" in health["controls"]
