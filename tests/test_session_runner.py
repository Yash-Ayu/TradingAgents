from tradingagents.runtime.session_runner import MarketSessionRunner


def test_session_runner_cycle_is_safe_and_returns_state():
    runner = MarketSessionRunner(broker_name="paper")
    result = runner.run_cycle({
        "is_market_open": True,
        "vix": 18.0,
        "drawdown_pct": 3.0,
        "trend_strength": 0.8,
        "atr_ratio": 0.7,
    }, symbol="NIFTY")

    assert result["status"] == "blocked"
    assert result["reason"] == "no_actionable_engine_decision"
    assert result["action"] == "hold"
