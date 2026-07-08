import asyncio
from playwright.async_api import async_playwright
from aiohttp import web

async def handle(request):
    return web.Response(text="<html><body>Hello</body></html>", content_type="text/html")

async def main():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = runner.addresses[0][1]

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        
        async def route_handler(route):
            print(f"Intercepted {route.request.url}")
            await route.continue_()
            
        await context.route("**/*", route_handler)
        
        page = await context.new_page()
        print(f"Going to http://127.0.0.1:{port}/")
        await page.goto(f"http://127.0.0.1:{port}/")
        print("Done!")
        
        await browser.close()
    await runner.cleanup()

asyncio.run(main())
