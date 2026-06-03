"""
browser.py – Playwright browser lifecycle helpers.

Responsibilities
----------------
* Launch a Chromium browser with anti-detection tweaks.
* Persist / restore LinkedIn session state (cookies + localStorage).
* Attach a console-log listener to every new page.
* Provide human-like random delays.
"""

from __future__ import annotations

import asyncio
import json
import random
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

try:
    from .config import (
        BROWSER_TIMEZONE,
        DIAGNOSTICS_DIR,
        HEADLESS,
        MAX_DELAY,
        MIN_DELAY,
        SESSION_PATH,
        USER_AGENT,
    )
except ImportError:
    from config import (
        BROWSER_TIMEZONE,
        DIAGNOSTICS_DIR,
        HEADLESS,
        MAX_DELAY,
        MIN_DELAY,
        SESSION_PATH,
        USER_AGENT,
    )


# ── Anti-detection JS injected into every page before scripts run ─────────────

_STEALTH_JS = """
// Remove the most obvious automation markers
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
window.chrome = { runtime: {} };
"""


# ── Public helpers ────────────────────────────────────────────────────────────

async def human_delay(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY) -> None:
    """Sleep for a random human-like interval."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def goto_with_retries(
    page: Page,
    url: str,
    *,
    timeout: int,
    wait_until: str = "domcontentloaded",
    attempts: int = 3,
    retry_delay_s: float = 15.0,
) -> None:
    """Navigate with bounded retries for transient network failures."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, timeout=timeout, wait_until=wait_until)
            return
        except Exception as exc:
            last_exc = exc
            message = str(exc)
            if "ERR_ABORTED" in message.upper():
                try:
                    current_url = page.url or ""
                except Exception:
                    current_url = ""
                if current_url and urlparse(current_url).netloc == urlparse(url).netloc:
                    logger.warning(f"Navigation aborted but page landed on the same host for {url}: {current_url}")
                    return
            if attempt >= attempts:
                break
            logger.warning(f"Navigation attempt {attempt}/{attempts} failed for {url}: {exc}")
            await asyncio.sleep(retry_delay_s)

    if last_exc:
        raise last_exc


async def build_context(
    playwright: Playwright,
    headless: bool = HEADLESS,
    console_buf: Optional[List[Dict[str, Any]]] = None,
    record_har: bool = True,
) -> tuple[Browser, BrowserContext]:
    """
    Launch Chromium and return (browser, context).

    Parameters
    ----------
    playwright  : Playwright instance from `async_playwright().__aenter__()`.
    headless    : Show/hide the browser window.
    console_buf : If provided, all console messages are appended here.
    record_har  : Record a HAR trace for the whole session.
    """
    har_path = str(DIAGNOSTICS_DIR / "session.har") if record_har else ""

    logger.info(f"Launching Chrome (headless={headless})")
    try:
        browser = await playwright.chromium.launch(
            channel="chrome",
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
                "--allow-running-insecure-content",
                "--disable-web-security",
            ],
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )
    except Exception as exc:
        logger.error(f"Chromium launch failed: {exc}")
        raise

    context_kwargs: Dict[str, Any] = dict(
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id=BROWSER_TIMEZONE,
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1"
        }
    )

    if har_path:
        context_kwargs["record_har_path"] = har_path

    # Restore saved LinkedIn session if it exists
    if SESSION_PATH.exists():
        try:
            context_kwargs["storage_state"] = str(SESSION_PATH)
            logger.info("Restoring saved LinkedIn session.")
        except Exception as exc:
            logger.warning(f"Could not restore session: {exc}")

    context = await browser.new_context(**context_kwargs)
    logger.info("Chromium browser context created.")

    # Inject stealth JS into every page before any other script runs
    await context.add_init_script(_STEALTH_JS)

    # Wire up console listener if a buffer was supplied
    if console_buf is not None:
        context.on("page", lambda p: _attach_console(p, console_buf))

    return browser, context


async def save_session(context: BrowserContext) -> None:
    """Persist cookies + localStorage for the current context."""
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(SESSION_PATH))
        logger.info(f"Session saved → {SESSION_PATH}")
    except Exception as exc:
        logger.warning(f"Failed to save session: {exc}")




def _attach_console(page: Page, buf: List[Dict[str, Any]]) -> None:
    """Append every console event from *page* to *buf*."""
    page.on(
        "console",
        lambda msg: buf.append({"type": msg.type, "text": msg.text}),
    )
    page.on(
        "pageerror",
        lambda err: buf.append({"type": "pageerror", "text": str(err)}),
    )


async def new_stealth_page(context):
    page = await context.new_page()
    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
    except ImportError:
        pass
    return page
