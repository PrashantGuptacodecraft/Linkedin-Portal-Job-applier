import asyncio
from backend.portal import autofill_portal_form
from backend.models import CandidateProfile
from playwright.async_api import async_playwright

async def test_pipeline():
    candidate = CandidateProfile(
        name="Test User",
        email="test@example.com",
        phone="5551234567",
        location="San Francisco, CA"
    )
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US"
        )
        
        print("Testing Appcast...")
        appcast_url = "https://click.appcast.io/t/GlqOPFOXGnpnWJXTRdC9k16_nW4uUKk3wAOhdalHnRQ="
        page, filled, status, outcome = await autofill_portal_form(context, appcast_url, candidate)
        print("Appcast final URL:", page.url)
        print("Appcast filled fields:", filled)
        await page.close()

        print("\nTesting Micro1...")
        micro1_url = "https://jobs.micro1.ai/post/03cd460b-81ae-46c1-9453-c92e57ee2c41?referralCode=e91c9585-63ad-45aa-9820-d63708190a83&utm_source=referral&utm_medium=share&utm_campaign=job_referral"
        page, filled, status, outcome = await autofill_portal_form(context, micro1_url, candidate)
        print("Micro1 Title:", await page.title())
        print("Micro1 filled fields:", filled)
        await page.close()

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_pipeline())
