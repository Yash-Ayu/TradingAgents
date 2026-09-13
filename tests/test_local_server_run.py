from tradingagents.runtime.__main__ import main
from tradingagents.runtime.local_server import start_local_server


def test_local_server_starts_and_returns_status():
    server = start_local_server()
    status = server.health()

    assert "status" in status
    assert "risk_state" in status
    assert "controls" in status


def test_runtime_main_starts_service():
    assert main() == 0
