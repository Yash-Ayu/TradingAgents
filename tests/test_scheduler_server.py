from tradingagents.runtime.scheduler_server import SchedulerServer


def test_scheduler_server_starts_and_stops_cleanly():
    server = SchedulerServer()

    started = server.start()
    assert started["status"] == "running"

    health = server.health()
    assert health["status"] == "running"
    assert "scheduler_status" in health

    stopped = server.stop()
    assert stopped["status"] == "stopped"
