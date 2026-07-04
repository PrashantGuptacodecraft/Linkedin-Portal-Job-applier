"""Ad-hoc diagnostic: open a LinkedIn URL with the app's browser config and
report what actually renders (redirects, stylesheet count, SPA mount, job cards).

Usage:
    .venv/Scripts/python.exe -m backend.diag_linkedin              # current flags
    .venv/Scripts/python.exe -m backend.diag_linkedin --clean      # without the SPA-breaking flags
"""
import asyncio
import sys
from playwright.async_api import async_playwright

URL = "https://www.linkedin.com/jobs/search/?keywords=Java+Developer+%2B+C2C&f_TPR=r86400&sortBy=DD&location=USA"


async def main(clean: bool):
    from backend.browser import build_context, _WIDGET_JS, _STEALTH_JS
    from backend import browser as B

    console = []
    async with async_playwright() as pw:
        if clean:
            # Monkeypatch: strip the SPA-breaking flags for this run only.
            import backend.browser as bmod
            orig = bmod.build_context
            # Simplest: build our own minimal context.
            browser = await pw.chromium.launch(channel="chrome", headless=False,
                args=["--disable-blink-features=AutomationControlled"])
            from backend.config import SESSION_PATH, USER_AGENT, BROWSER_TIMEZONE
            kwargs = dict(user_agent=USER_AGENT, viewport={"width":1440,"height":900},
                          locale="en-US", timezone_id=BROWSER_TIMEZONE)
            if SESSION_PATH.exists():
                kwargs["storage_state"] = str(SESSION_PATH)
            context = await browser.new_context(**kwargs)
            await context.add_init_script(_WIDGET_JS)
        else:
            browser, context = await build_context(pw, headless=False, console_buf=console, record_har=False)

        page = await context.new_page()
        page.on("pageerror", lambda e: console.append({"type": "pageerror", "text": str(e)[:200]}))
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(4)

        info = await page.evaluate("""() => ({
            url: location.href,
            title: document.title,
            styleSheets: document.styleSheets.length,
            linkTags: document.querySelectorAll('link[rel=stylesheet]').length,
            scripts: document.querySelectorAll('script').length,
            bodyTextLen: (document.body && document.body.innerText || '').length,
            appRoot: !!document.querySelector('#main, .scaffold-layout, .application-outlet'),
            appRootChildren: (document.querySelector('.application-outlet, #main') || {childElementCount:0}).childElementCount,
            jobCards: document.querySelectorAll('.job-card-container, li.jobs-search-results__list-item, [data-job-id], .scaffold-layout__list-item').length,
            authwall: /authwall|\\/login|checkpoint|guest/i.test(location.href),
        })""")

        print(f"\n===== RESULT ({'CLEAN flags' if clean else 'CURRENT flags'}) =====")
        for k, v in info.items():
            print(f"  {k:18}: {v}")
        errs = [c for c in console if c.get("type") in ("error", "pageerror")]
        print(f"  console_errors    : {len(errs)}")
        for e in errs[:8]:
            print(f"     - {e['text'][:160]}")

        shot = f"screenshots/diag_linkedin_{'clean' if clean else 'current'}.png"
        import os
        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path=shot, full_page=True)
        print(f"  screenshot        : {shot}")

        # Verdict heuristic
        if info["authwall"]:
            print("\n  VERDICT: session invalid → LinkedIn auth wall (re-login needed).")
        elif info["styleSheets"] < 3 or info["appRootChildren"] == 0:
            print("\n  VERDICT: SPA did NOT hydrate (only HTML shell) — launch flags / CSP breakage.")
        elif info["jobCards"] > 0:
            print("\n  VERDICT: renders fine, job cards present.")
        else:
            print("\n  VERDICT: page rendered but no job cards (maybe logged-out search limits).")

        await asyncio.sleep(1)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main("--clean" in sys.argv))
