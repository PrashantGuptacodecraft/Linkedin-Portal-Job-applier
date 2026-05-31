import asyncio
import traceback
from pathlib import Path

async def run():
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        print("IMPORT_ERROR", e)
        traceback.print_exc()
        return

    out_dir = Path("data/diagnostics/test_playwright")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            print("LAUNCH_OK: navigating to example.com")
            await page.goto("https://example.com", timeout=30000)
            screenshot_path = out_dir / "example.png"
            await page.screenshot(path=str(screenshot_path))
            print("SCREENSHOT_SAVED", screenshot_path)
            await browser.close()
            print("BROWSER_CLOSED")
    except Exception as e:
        print("RUNTIME_ERROR", e)
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(run())
