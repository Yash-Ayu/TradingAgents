"""Optional headless browser check of a running demo paper desk.

Install `pip install playwright` and `python -m playwright install chromium`.
Run this only against a disposable --source demo ledger; it changes its controls.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--output', type=Path, default=Path('runtime_state/dashboard-check.png'))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        page.goto(args.url)
        expect(page.get_by_role('heading', name='Paper desk.')).to_be_visible()
        state = page.request.get(args.url + '/api/status').json()
        if state['source'] != 'demo' or state['mode'] != 'paper':
            raise RuntimeError('Verification requires a disposable demo paper desk')
        expect(page.locator('#state')).to_have_text('stopped')
        page.get_by_role('button', name='Start monitoring').click()
        expect(page.locator('#state')).to_have_text('running')
        expect(page.locator('#price')).to_contain_text('103.50', timeout=10000)
        expect(page.locator('#engine')).to_have_text('Engine off')
        expect(page.locator('#orders')).to_contain_text('No paper orders yet')
        page.screenshot(path=str(args.output), full_page=True)
        page.get_by_role('button', name='Pause', exact=True).click()
        expect(page.locator('#state')).to_have_text('stopped')
        page.once('dialog', lambda dialog: dialog.accept())
        page.get_by_role('button', name='Emergency stop', exact=True).click()
        expect(page.locator('#state')).to_have_text('Emergency stop')
        page.reload()
        expect(page.locator('#state')).to_have_text('Emergency stop')
        expect(page.get_by_role('button', name='Start monitoring')).to_be_disabled()
        page.get_by_role('button', name='Reset stop').click()
        expect(page.locator('#state')).to_have_text('stopped')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(args.output.with_name('dashboard-mobile.png')), full_page=True)
        assert not errors, errors
        browser.close()
    print('Browser PASS: render, start, pause, persistent emergency stop, reset, mobile layout; no console errors')


if __name__ == '__main__':
    main()
