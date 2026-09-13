from tradingagents.runtime import MarketScheduler, SafeExecutionLoop


def test_execution_loop_blocks_risky_cycle():
    loop = SafeExecutionLoop()
    result = loop.run_cycle({
        "is_market_open": True,
        "vix": 40.0,
        "drawdown_pct": 12.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.0,
    })

    assert result["trade_allowed"] is False
    assert result["action"] == "flatten"
    assert result["status"] == "risk_blocked"


def test_scheduler_tracks_safe_trade_window():
    scheduler = MarketScheduler(interval_seconds=30)
    assert scheduler.start() == "running"

    safe_result = scheduler.tick({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    })

    assert safe_result["trade_allowed"] is True
    assert safe_result["action"] == "monitor"
    assert scheduler.stop() == "stopped"
