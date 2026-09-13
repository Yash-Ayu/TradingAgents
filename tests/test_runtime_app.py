from tradingagents.runtime.local_app import TradingAppShell


def test_app_shell_exposes_status_and_controls():
    shell = TradingAppShell()
    started = shell.start()
    status = shell.get_status()

    assert started == "running"
    assert status["status"] == "running"
    assert "risk_state" in status
    assert "allow_trade" in status

    stopped = shell.stop()
    assert stopped == "stopped"
    assert shell.get_status()["status"] == "stopped"
