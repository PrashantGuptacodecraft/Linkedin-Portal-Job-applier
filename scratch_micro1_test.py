import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://jobs.micro1.ai/post/03cd460b-81ae-46c1-9453-c92e57ee2c41")
        # Wait for form to load
        await page.wait_for_selector("input", timeout=10000)
        
        # Get all inputs
        inputs = await page.query_selector_all("input")
        for i in inputs:
            html = await i.evaluate("el => el.outerHTML")
            print(html)
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
