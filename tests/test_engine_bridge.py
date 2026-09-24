from unittest.mock import MagicMock

from tradingagents.runtime.engine_bridge import SafeTradingEngineBridge


def test_engine_bridge_blocks_risky_market():
    graph = MagicMock()
    bridge = SafeTradingEngineBridge(graph_factory=lambda: graph)

    result = bridge.run(
        symbol="NIFTY",
        start_date="2025-01-01",
        end_date="2026-09-01",
        snapshot={
            "is_market_open": True,
            "vix": 42.0,
            "drawdown_pct": 14.0,
            "trend_strength": 0.2,
            "atr_ratio": 2.1,
        },
    )

    assert result["allowed"] is False
    assert result["action"] == "flatten"
    graph.propagate.assert_not_called()


def test_engine_bridge_runs_when_safe():
    graph = MagicMock()
    graph.propagate.return_value = ("report", "BUY")
    bridge = SafeTradingEngineBridge(graph_factory=lambda: graph)

    result = bridge.run(
        symbol="NIFTY",
        start_date="2025-01-01",
        end_date="2026-09-01",
        snapshot={
            "is_market_open": True,
            "vix": 18.0,
            "drawdown_pct": 3.0,
            "trend_strength": 0.8,
            "atr_ratio": 0.7,
        },
    )

    assert result["allowed"] is True
    assert result["action"] == "monitor"
    assert result["decision"] == "BUY"
    graph.propagate.assert_called_once_with("NIFTY", "2026-09-01", asset_type="stock")
