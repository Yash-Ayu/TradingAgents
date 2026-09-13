from tradingagents.runtime.browser_dashboard import BrowserDashboard


def test_browser_dashboard_renders_status_html():
    dashboard = BrowserDashboard()
    html = dashboard.render()

    assert "<html" in html.lower()
    assert "status" in html.lower()
    assert "trading" in html.lower()
