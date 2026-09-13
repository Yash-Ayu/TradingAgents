from tradingagents.runtime.safe_runtime import RiskGate, SafeTradingRuntime


def test_risk_gate_blocks_risky_intraday_conditions():
    gate = RiskGate()
    snapshot = {
        "is_market_open": True,
        "vix": 42.0,
        "drawdown_pct": 14.0,
        "trend_strength": 0.2,
        "atr_ratio": 2.1,
    }

    assert gate.should_allow_trade(snapshot) is False
    assert gate.should_flatten_positions(snapshot) is True
    assert gate.evaluate(snapshot)["risk_state"] in {"high", "elevated"}


def test_risk_gate_allows_safe_intraday_conditions():
    gate = RiskGate()
    snapshot = {
        "is_market_open": True,
        "vix": 16.0,
        "drawdown_pct": 4.0,
        "trend_strength": 0.75,
        "atr_ratio": 0.9,
    }

    assert gate.should_allow_trade(snapshot) is True
    assert gate.should_flatten_positions(snapshot) is False
    assert gate.evaluate(snapshot)["risk_state"] == "normal"


def test_runtime_starts_and_stops_cleanly():
    runtime = SafeTradingRuntime()
    assert runtime.start() == "running"
    assert runtime.stop() == "stopped"
    status = runtime.get_status()
    assert status["status"] == "stopped"
    assert "risk_state" in status
