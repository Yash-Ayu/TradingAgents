from datetime import datetime

from tradingagents.runtime import MarketSessionMonitor, TradingDashboard


def test_market_session_monitor_detects_market_hours():
    monitor = MarketSessionMonitor("india_nse")

    open_time = datetime(2026, 9, 11, 9, 15, 0)
    close_time = datetime(2026, 9, 11, 15, 31, 0)

    assert monitor.is_market_open(open_time) is True
    assert monitor.is_market_open(close_time) is False


def test_dashboard_reports_market_status_and_controls():
    dashboard = TradingDashboard()
    started = dashboard.start()
    status = dashboard.get_status()

    assert started == "running"
    assert status["status"] == "running"
    assert "market_open" in status
    assert "risk_state" in status
    assert "scheduler_status" in status
