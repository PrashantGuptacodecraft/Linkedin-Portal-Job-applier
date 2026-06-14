import asyncio
from playwright.async_api import async_playwright

async def test_urls():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        # Test Micro1
        print("Testing Micro1...")
        try:
            await page.goto("https://jobs.micro1.ai/post/03cd460b-81ae-46c1-9453-c92e57ee2c41", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            print("Micro1 Title:", await page.title())
            html = await page.content()
            print("Micro1 Content snippet:", html[:500])
        except Exception as e:
            print("Micro1 error:", e)

        # Test Appcast
        print("\nTesting Appcast...")
        try:
            await page.goto("https://click.appcast.io/t/GlqOPFOXGnpnWJXTRdC9k16_nW4uUKk3wAOhdalHnRQ=", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            print("Appcast Title:", await page.title())
            print("Appcast final URL:", page.url)
            html = await page.content()
            print("Appcast Content snippet:", html[:500])
        except Exception as e:
            print("Appcast error:", e)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_urls())
