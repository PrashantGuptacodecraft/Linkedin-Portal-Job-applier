from playwright.sync_api import sync_playwright
import sys

with sync_playwright() as p:
    try:
        print('Launching Chromium (headed smoke test)...')
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto('https://example.com', timeout=30000)
        page.screenshot(path='data/diagnostics/smoke_example.png')
        print('Screenshot saved: data/diagnostics/smoke_example.png')
        browser.close()
        print('Browser closed — smoke test OK')
    except Exception as e:
        print('Smoke test failed:', e)
        sys.exit(1)
