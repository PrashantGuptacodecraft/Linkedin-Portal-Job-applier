import asyncio
import os
from browser import build_context
from playwright.async_api import async_playwright

URL = 'https://www.linkedin.com/talentcommunity/apply/1396106333/?locale=en_US'

async def main():
    async with async_playwright() as pw:
        browser, context = await build_context(pw, headless=False, console_buf=None, record_har=False)
        page = await context.new_page()
        try:
            await page.goto(URL, timeout=60000)
            await page.wait_for_load_state('domcontentloaded')
            print('PAGE URL:', page.url)
            anchors = await page.query_selector_all('a')
            print('--- anchors with hrefs ---')
            for a in anchors:
                href = await a.get_attribute('href')
                text = (await a.inner_text()).strip()[:80]
                if href:
                    print(href, '->', text)
            print('--- frames ---')
            for f in page.frames:
                try:
                    print('Frame:', f.url)
                except Exception:
                    pass
            print('--- input types ---')
            inputs = await page.query_selector_all('input')
            for inp in inputs[:80]:
                try:
                    t = await inp.get_attribute('type')
                    name = await inp.get_attribute('name')
                    aria = await inp.get_attribute('aria-label')
                    print('INPUT', t, name, aria)
                except Exception:
                    pass
        finally:
            await context.close()
            await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
