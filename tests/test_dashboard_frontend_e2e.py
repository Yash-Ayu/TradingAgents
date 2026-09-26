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

import threading

import pytest
from playwright.sync_api import sync_playwright

from tradingagents.runtime.angel_data import DemoFeed
from tradingagents.runtime.dashboard_server import DashboardServer
from tradingagents.runtime.paper_ledger import PaperLedger
from tradingagents.runtime.paper_service import PaperTradingService


@pytest.fixture(scope="module")
def running_dashboard(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("dashboard_test")
    ledger = PaperLedger(tmp_path / "test_desk.sqlite3", initial_cash=100000)
    feed = DemoFeed()
    service = PaperTradingService(feed, ledger)
    server = DashboardServer(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server

    service.stop()
    if service.thread:
        service.thread.join(timeout=2)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    ledger.close()


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

