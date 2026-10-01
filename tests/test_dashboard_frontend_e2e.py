"""
End-to-End Headless Browser Integration Test for TradingAgents Dashboard.
Verifies all user-reported frontend interactions:
- Tabs switching (Markets, Positions, Orders, Funds in both topbar & portfolio-tabs)
- Watchlist search, add ticker with '+', and item selection
- Interactive SVG Price Chart (Candles and Line modes)
- AI Setup dialog open and close
- Session runner controls (Start demo, Pause)
- Zero JavaScript console errors or unhandled exceptions
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from playwright.sync_api import sync_playwright

from tradingagents.runtime.angel_data import DemoFeed
from tradingagents.runtime.dashboard_server import DashboardServer
from tradingagents.runtime.market_snapshot import IST
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService


class OfflineTestMarketAdapter:
    """100% offline deterministic market adapter for E2E frontend tests.

    Completely decouples browser E2E tests from Angel One and Yahoo Finance APIs,
    guaranteeing zero external network calls, zero rate-limiting, and 100% deterministic latency.
    """

    name = "offline_test"

    def __init__(self):
        self.candle_calls = 0
        self.quote_calls = 0

    def is_configured(self) -> bool:
        return True

    def get_candles(self, symbol: str, interval: str = "5m") -> dict[str, Any]:
        self.candle_calls += 1
        now = datetime(2026, 9, 29, 15, 30, 0, tzinfo=IST)
        minute = now.replace(second=0, microsecond=0)
        step_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}
        step_minutes = step_map.get(interval, 5)
        count = 60
        rows = []
        base_price = 500.0 if "SBIN" in symbol else (2500.0 if "RELIANCE" in symbol else 100.0)
        for i in range(count):
            stamp = minute - timedelta(minutes=(count - i) * step_minutes)
            wave = math.sin(i * 0.3) * 1.5 + (i * 0.08)
            o = round(base_price + wave, 2)
            c = round(base_price + wave + (0.35 if (i % 3 != 0) else -0.4), 2)
            h = round(max(o, c) + 0.25, 2)
            low_p = round(min(o, c) - 0.20, 2)
            v = int(1200 + (i % 7) * 180 + abs(math.sin(i)) * 400)
            rows.append([stamp.isoformat(), o, h, low_p, c, v])
        return {
            "symbol": symbol,
            "source": "OFFLINE_TEST_ADAPTER",
            "interval": interval,
            "chart": rows,
            "price": rows[-1][4],
            "timestamp": rows[-1][0],
            "change_pct": round((rows[-1][4] / rows[0][1] - 1) * 100, 2),
            "is_market_open": False,
            "market_status": "CLOSED",
        }

    def get_quote(self, symbol: str) -> dict[str, Any]:
        self.quote_calls += 1
        candles = self.get_candles(symbol, interval="5m")
        return {
            "symbol": symbol,
            "ltp": candles["price"],
            "source": "OFFLINE_TEST_ADAPTER",
            "timestamp": candles["timestamp"],
            "is_market_open": False,
        }


@pytest.fixture(scope="module")
def running_dashboard(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("dashboard_test")
    ledger = PaperLedger(tmp_path / "test_desk.sqlite3", initial_cash=100000)
    feed = DemoFeed()
    service = PaperTradingService(feed, ledger)
    fake_adapter = OfflineTestMarketAdapter()

    patcher_adapter = patch("tradingagents.runtime.dashboard_server.get_active_market_adapter", return_value=fake_adapter)

    def forbidden_research_chart(*args, **kwargs):
        raise AssertionError("research_chart external network access forbidden during frontend E2E tests")

    patcher_research = patch("tradingagents.runtime.dashboard_server.research_chart", side_effect=forbidden_research_chart)

    patcher_adapter.start()
    patcher_research.start()

    server = DashboardServer(service, port=0)
    server.test_adapter = fake_adapter
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield server
    finally:
        service.stop()
        if service.thread:
            service.thread.join(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        ledger.close()
        patcher_research.stop()
        patcher_adapter.stop()


def test_dashboard_frontend_full_e2e(running_dashboard):
    errors = []
    server_url = running_dashboard.origin

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        page.on("pageerror", lambda err: errors.append(f"PageError: {err}"))
        page.on("console", lambda msg: errors.append(f"ConsoleError: {msg.text}") if msg.type == "error" else None)

        # 1. Load Dashboard
        response = page.goto(server_url)
        assert response.status == 200

        # Wait for page to initialize
        page.wait_for_selector(".topbar")
        page.wait_for_selector("#panel-overview")

        # =========================================================================
        # 2. TEST TABS SWITCHING (TOPBAR & PORTFOLIO TABS)
        # =========================================================================
        # Initially, Overview panel should be visible, others hidden
        assert page.is_visible("#panel-overview")
        assert not page.is_visible("#panel-positions")
        assert not page.is_visible("#panel-orders")
        assert not page.is_visible("#panel-funds")

        # Click Positions in Topbar
        page.locator('.topbar nav button.nav[data-tab="positions"]').click()
        page.wait_for_timeout(200)
        assert not page.is_visible("#panel-overview")
        assert page.is_visible("#panel-positions")
        assert not page.is_visible("#panel-orders")
        assert not page.is_visible("#panel-funds")
        assert "active" in page.locator('.topbar nav button.nav[data-tab="positions"]').get_attribute("class")

        # Click Orders in Topbar
        page.locator('.topbar nav button.nav[data-tab="orders"]').click()
        page.wait_for_timeout(200)
        assert not page.is_visible("#panel-positions")
        assert page.is_visible("#panel-orders")
        assert "active" in page.locator('.topbar nav button.nav[data-tab="orders"]').get_attribute("class")

        # Click Funds in Topbar
        page.locator('.topbar nav button.nav[data-tab="funds"]').click()
        page.wait_for_timeout(200)
        assert not page.is_visible("#panel-orders")
        assert page.is_visible("#panel-funds")
        assert "active" in page.locator('.topbar nav button.nav[data-tab="funds"]').get_attribute("class")

        # Click Markets in Topbar (Switches back to overview)
        page.locator('.topbar nav button.nav[data-tab="overview"]').click()
        page.wait_for_timeout(200)
        assert page.is_visible("#panel-overview")
        assert not page.is_visible("#panel-funds")
        assert "active" in page.locator('.topbar nav button.nav[data-tab="overview"]').get_attribute("class")

        # Test Portfolio Tablist Buttons directly
        page.locator('.portfolio-tabs button[data-tab="positions"]').click()
        page.wait_for_timeout(200)
        assert page.is_visible("#panel-positions")
        assert page.locator('.portfolio-tabs button[data-tab="positions"]').get_attribute("aria-selected") == "true"

        page.locator('.portfolio-tabs button[data-tab="orders"]').click()
        page.wait_for_timeout(200)
        assert page.is_visible("#panel-orders")

        page.locator('.portfolio-tabs button[data-tab="funds"]').click()
        page.wait_for_timeout(200)
        assert page.is_visible("#panel-funds")

        page.locator('.portfolio-tabs button[data-tab="overview"]').click()
        page.wait_for_timeout(200)
        assert page.is_visible("#panel-overview")

        # =========================================================================
        # 3. TEST WATCHLIST, SEARCH, AND ADD TICKER WITH '+'
        # =========================================================================
        # Check initial watchlist items exist
        watch_rows = page.locator(".watch-row")
        initial_count = watch_rows.count()
        assert initial_count > 0

        # Type ticker into search and click '+'
        page.fill("#watch-search", "WIPRO")
        page.locator('#watch-form button[type="submit"]').click()
        page.wait_for_timeout(300)

        # Verify WIPRO.NS is now in watchlist and selected
        new_count = page.locator(".watch-row").count()
        assert new_count == initial_count + 1
        assert "WIPRO.NS" in page.locator("#chart-symbol").text_content()
        assert "WIPRO.NS" in page.locator("#analysis-symbol").text_content()

        # Click on an existing stock row in watchlist (e.g. SBIN.NS)
        sbin_btn = page.locator('.watch-row:has-text("SBIN.NS")').first
        sbin_btn.click()
        page.wait_for_timeout(500)
        assert "SBIN.NS" in page.locator("#chart-symbol").text_content()
        assert "SBIN.NS" in page.locator("#analysis-symbol").text_content()
        assert not page.locator("#analyze").is_disabled()

        # =========================================================================
        # 4. TEST TRADINGVIEW INTERACTIVE CHART, INTERVALS & FLOATING LEGEND
        # =========================================================================
        # Select DEMO-EQ to test synthetic candles
        demo_btn = page.locator('.watch-row:has-text("DEMO-EQ")').first
        if demo_btn.count() > 0:
            demo_btn.click()
        else:
            page.fill("#watch-search", "DEMO-EQ")
            page.locator('#watch-form button[type="submit"]').click()
        page.wait_for_timeout(600)

        # TradingView Chart container and Canvas should have rendered
        tv_chart = page.locator("#tv-chart")
        assert tv_chart.is_visible()
        # TradingView renders its chart layers onto HTML5 <canvas> elements
        canvases = page.locator("#tv-chart canvas")
        assert canvases.count() > 0, "TradingView chart canvas should be rendered"

        # Verify Floating OHLCV Legend is visible and populated
        legend = page.locator("#tv-legend")
        assert legend.is_visible()
        assert "DEMO-EQ" in page.locator("#leg-symbol").text_content()
        assert page.locator("#leg-c").text_content() != "—"

        # Test Timeframe Interval Switching (e.g. 15m)
        btn_15m = page.locator('.interval-btn[data-interval="15m"]')
        btn_15m.click()
        page.wait_for_timeout(400)
        assert "active" in btn_15m.get_attribute("class")
        assert "15m" in page.locator("#chart-interval").text_content()
        assert "15m" in page.locator("#leg-interval").text_content()

        # Test Timeframe Interval Switching (e.g. 1h)
        btn_1h = page.locator('.interval-btn[data-interval="1h"]')
        btn_1h.click()
        page.wait_for_timeout(400)
        assert "active" in btn_1h.get_attribute("class")
        assert "1h" in page.locator("#chart-interval").text_content()

        # Switch back to 5m
        btn_5m = page.locator('.interval-btn[data-interval="5m"]')
        btn_5m.click()
        page.wait_for_timeout(400)
        assert "active" in btn_5m.get_attribute("class")

        # Toggle Area mode
        page.locator('.chart-toggle[data-chart="area"]').click()
        page.wait_for_timeout(200)
        assert "active" in page.locator('.chart-toggle[data-chart="area"]').get_attribute("class")

        # Toggle Bars mode
        page.locator('.chart-toggle[data-chart="bars"]').click()
        page.wait_for_timeout(200)
        assert "active" in page.locator('.chart-toggle[data-chart="bars"]').get_attribute("class")

        # Toggle to Line mode
        page.locator('.chart-toggle[data-chart="line"]').click()
        page.wait_for_timeout(200)
        assert "active" in page.locator('.chart-toggle[data-chart="line"]').get_attribute("class")

        # Toggle back to Candles mode
        page.locator('.chart-toggle[data-chart="candles"]').click()
        page.wait_for_timeout(200)
        assert "active" in page.locator('.chart-toggle[data-chart="candles"]').get_attribute("class")

        # Test Indicators: EMA 20, EMA 50, VWAP
        ema20_btn = page.locator('.indicator-btn[data-indicator="ema20"]')
        ema20_btn.click()
        page.wait_for_timeout(200)
        assert "active" in ema20_btn.get_attribute("class")
        assert page.is_visible("#leg-ema20")
        assert page.locator("#val-ema20").text_content() != "—"

        ema50_btn = page.locator('.indicator-btn[data-indicator="ema50"]')
        ema50_btn.click()
        page.wait_for_timeout(200)
        assert "active" in ema50_btn.get_attribute("class")
        assert page.is_visible("#leg-ema50")

        vwap_btn = page.locator('.indicator-btn[data-indicator="vwap"]')
        vwap_btn.click()
        page.wait_for_timeout(200)
        assert "active" in vwap_btn.get_attribute("class")
        assert page.is_visible("#leg-vwap")

        # Test Theme Toggle (Dark / Light)
        theme_btn = page.locator("#chart-theme-btn")
        theme_btn.click()
        page.wait_for_timeout(200)
        assert "dark-theme" in page.locator(".chart-panel").get_attribute("class")
        theme_btn.click()
        page.wait_for_timeout(200)
        assert "dark-theme" not in (page.locator(".chart-panel").get_attribute("class") or "")

        # Test Fullscreen Toggle
        fs_btn = page.locator("#chart-fullscreen-btn")
        fs_btn.click()
        page.wait_for_timeout(200)
        assert "fullscreen" in page.locator(".chart-panel").get_attribute("class")
        fs_btn.click()
        page.wait_for_timeout(200)
        assert "fullscreen" not in (page.locator(".chart-panel").get_attribute("class") or "")

        # Test Fit / Reset Zoom Button
        fit_btn = page.locator("#chart-fit-btn")
        if fit_btn.is_visible():
            fit_btn.click()
            page.wait_for_timeout(200)

        # Hover over canvas to trigger crosshairs and legend tracking
        tv_chart.hover(position={"x": 200, "y": 150})
        page.wait_for_timeout(200)
        assert page.locator("#leg-c").text_content() != "—"

        # =========================================================================
        # 5. TEST AI SETUP DIALOG
        # =========================================================================
        page.locator("#setup-top").click()
        page.wait_for_timeout(200)
        assert page.locator("#ai-dialog").get_attribute("open") is not None

        # Close dialog
        page.locator("#close-ai").click()
        page.wait_for_timeout(200)
        assert page.locator("#ai-dialog").get_attribute("open") is None

        # =========================================================================
        # 6. TEST SESSION RUNNER CONTROLS
        # =========================================================================
        # Click Start demo
        start_btn = page.locator("#start")
        if not start_btn.is_disabled():
            start_btn.click()
            page.wait_for_timeout(500)
            assert page.locator("#state").text_content().strip() in ["Running", "running"]

        # Click Pause
        stop_btn = page.locator("#stop")
        if not stop_btn.is_disabled():
            stop_btn.click()
            page.wait_for_timeout(500)
            assert page.locator("#state").text_content().strip() in ["Paused", "stopped"]

        # =========================================================================
        # 7. ZERO JAVASCRIPT CONSOLE ERRORS
        # =========================================================================
        browser.close()

    assert not errors, f"Browser Console Errors occurred: {errors}"
    assert running_dashboard.test_adapter.candle_calls > 0, "Expected fake adapter to service chart calls"


@pytest.mark.parametrize(
    ("width", "height", "device_name"),
    [
        (320, 568, "iPhone SE 1st gen / Compact"),
        (360, 640, "Standard Android small"),
        (375, 667, "iPhone SE 2nd/3rd gen"),
        (390, 844, "iPhone 12/13/14"),
        (412, 915, "Samsung Galaxy / Pixel"),
        (430, 932, "iPhone 14/15/16 Pro Max"),
        (768, 1024, "iPad / Tablet Portrait"),
        (1440, 900, "Desktop HD"),
    ],
)
def test_mobile_responsive_viewports(running_dashboard, width, height, device_name):
    """
    Verifies mobile responsive layout across all standard device viewports:
    - Zero document-level horizontal overflow (scrollWidth <= clientWidth + 1)
    - Runner card prioritized above main-area on mobile viewports (<= 768px)
    - Onboarding steps wrap cleanly without collision/overlap
    - Desktop grid layout preserved on wide viewports (1440px)
    - Zero browser console errors
    """
    errors = []
    server_url = running_dashboard.origin

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height})
        page = context.new_page()

        page.on("console", lambda msg: errors.append(f"Console {msg.type}: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda err: errors.append(f"Page Error: {err}"))

        page.goto(server_url, wait_until="networkidle")
        page.wait_for_timeout(300)

        # 1. Zero document-level horizontal overflow
        overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, (
            f"Horizontal page overflow detected at {width}px ({device_name}): "
            f"scrollWidth={page.evaluate('() => document.documentElement.scrollWidth')}, "
            f"clientWidth={page.evaluate('() => document.documentElement.clientWidth')}"
        )

        # 2. Layout checks
        if width <= 768:
            # Runner card should be visually prioritized above main area
            runner_box = page.locator(".runner-card").bounding_box()
            main_box = page.locator(".main-area").bounding_box()
            assert runner_box and main_box, "Runner card and main area must exist"
            assert runner_box["y"] < main_box["y"], (
                f"Runner card should have high priority above main-area on mobile at {width}px: "
                f"runner_y={runner_box['y']}, main_y={main_box['y']}"
            )

            # Onboarding steps should stack vertically without overlap
            steps = page.locator(".step-card").all()
            assert len(steps) == 3, f"Should have 3 onboarding step cards, got {len(steps)}"
            boxes = [s.bounding_box() for s in steps]
            assert boxes[0]["y"] + boxes[0]["height"] <= boxes[1]["y"] + 2, "Step 1 and 2 overlap"
            assert boxes[1]["y"] + boxes[1]["height"] <= boxes[2]["y"] + 2, "Step 2 and 3 overlap"
        else:
            # Desktop layout preservation
            workspace_cols = page.evaluate(
                "() => window.getComputedStyle(document.querySelector('.workspace')).gridTemplateColumns.split(' ').length"
            )
            assert workspace_cols == 3, f"Desktop should have 3 columns, got {workspace_cols}"

        browser.close()

    assert not errors, f"Browser errors occurred at {width}px ({device_name}): {errors}"


def test_e2e_isolation_blocks_external_network(running_dashboard):
    """Verifies that the dashboard under test routes market requests through OfflineTestMarketAdapter
    and never calls real Angel One or Yahoo network endpoints."""
    assert running_dashboard.test_adapter.candle_calls > 0
    assert running_dashboard.test_adapter.name == "offline_test"
