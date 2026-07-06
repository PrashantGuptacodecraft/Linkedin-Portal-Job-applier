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
        USE_PATCHRIGHT,
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
        USE_PATCHRIGHT,
    )


# ── Anti-detection JS injected into every page before scripts run ─────────────

# navigator/webdriver spoof — only injected when Patchright is NOT active
# (Patchright patches these natively; double-spoofing re-introduces detectable
# leaks). See config.USE_PATCHRIGHT.
_WEBDRIVER_SPOOF_JS = """
// Remove the most obvious automation markers
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins',   { get: () => [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'mhjimiigfkceipb', description: '' },
    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
window.chrome = { runtime: {} };
"""

# Pause/resume UI widget — REQUIRED by the pause system
# (window.showAntigravityPauseUI / hideAntigravityPauseUI / antigravityResumeBot).
# Always injected regardless of stealth mode.
_WIDGET_JS = """
if (window === window.top) {
    window.addEventListener('DOMContentLoaded', () => {
        if (document.getElementById('antigravity-bot-widget')) return;

        const widget = document.createElement('div');
        widget.id = 'antigravity-bot-widget';
        widget.style.cssText = `
            position: fixed; bottom: 16px; right: 16px; z-index: 2147483647;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        `;
        widget.innerHTML = `
            <style>
                #ag-dot { width: 18px; height: 18px; border-radius: 50%; background: #22c55e;
                    box-shadow: 0 0 8px rgba(34,197,94,0.6); transition: all 0.3s; cursor: pointer;
                    display: flex; align-items: center; justify-content: center; font-size: 10px; color: white; }
                #ag-dot.paused { background: #ef4444; box-shadow: 0 0 12px rgba(239,68,68,0.7);
                    animation: ag-pulse 1.5s infinite; }
                @keyframes ag-pulse { 0%,100%{box-shadow:0 0 8px rgba(239,68,68,0.5)} 50%{box-shadow:0 0 18px rgba(239,68,68,0.9)} }
                #ag-panel { display: none; background: rgba(15,15,25,0.92); backdrop-filter: blur(12px);
                    border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; padding: 12px 16px;
                    min-width: 220px; margin-bottom: 8px; color: #e2e8f0; font-size: 13px;
                    box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
                #ag-panel.visible { display: block; animation: ag-fadeIn 0.2s ease-out; }
                @keyframes ag-fadeIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
                #ag-status { font-weight: 600; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
                #ag-reason { font-size: 12px; color: #94a3b8; margin-bottom: 10px; line-height: 1.4;
                    max-height: 60px; overflow-y: auto; }
                #ag-resume-btn { display: none; width: 100%; padding: 8px; border: none; border-radius: 8px;
                    background: linear-gradient(135deg, #22c55e, #16a34a); color: white; font-weight: 700;
                    font-size: 13px; cursor: pointer; transition: transform 0.1s, box-shadow 0.2s;
                    box-shadow: 0 2px 8px rgba(34,197,94,0.3); }
                #ag-resume-btn:hover { transform: scale(1.03); box-shadow: 0 4px 16px rgba(34,197,94,0.5); }
                #ag-resume-btn.visible { display: block; }
            </style>
            <div id="ag-panel">
                <div id="ag-status"><span id="ag-status-dot" style="width:8px;height:8px;border-radius:50%;background:#22c55e;"></span><span id="ag-status-text">Bot Active</span></div>
                <div id="ag-reason"></div>
                <button id="ag-resume-btn">▶ RESUME BOT</button>
            </div>
            <div id="ag-dot" title="Antigravity Bot">⚡</div>
        `;

        (document.body || document.documentElement).appendChild(widget);

        const dot = document.getElementById('ag-dot');
        const panel = document.getElementById('ag-panel');
        let panelOpen = false;

        dot.addEventListener('click', () => {
            panelOpen = !panelOpen;
            panel.classList.toggle('visible', panelOpen);
        });

        document.getElementById('ag-resume-btn').addEventListener('click', async () => {
            document.getElementById('ag-status-text').textContent = 'Resuming...';
            document.getElementById('ag-resume-btn').style.display = 'none';
            try { if (window.antigravityResumeBot) await window.antigravityResumeBot(); } catch(e) { console.error(e); }
        });

        window.showAntigravityPauseUI = (reason) => {
            dot.classList.add('paused');
            dot.textContent = '⏸';
            document.getElementById('ag-status-dot').style.background = '#ef4444';
            document.getElementById('ag-status-text').textContent = 'PAUSED';
            document.getElementById('ag-reason').textContent = reason || 'Manual action required';
            document.getElementById('ag-resume-btn').classList.add('visible');
            panel.classList.add('visible');
            panelOpen = true;
        };

        window.hideAntigravityPauseUI = () => {
            dot.classList.remove('paused');
            dot.textContent = '⚡';
            document.getElementById('ag-status-dot').style.background = '#22c55e';
            document.getElementById('ag-status-text').textContent = 'Bot Active';
            document.getElementById('ag-reason').textContent = '';
            document.getElementById('ag-resume-btn').classList.remove('visible');
            panel.classList.remove('visible');
            panelOpen = false;
        };
    });
}
"""

# Full stealth script used when Patchright is NOT installed (spoof + widget).
_STEALTH_JS = _WEBDRIVER_SPOOF_JS + _WIDGET_JS


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
    session_id: str = "default",
) -> tuple[Browser, BrowserContext]:
    """
    Launch Chromium and return (browser, context).

    Parameters
    ----------
    playwright  : Playwright instance from `async_playwright().__aenter__()`.
    headless    : Show/hide the browser window.
    console_buf : If provided, all console messages are appended here.
    record_har  : Record a HAR trace for the whole session.
    session_id  : The session ID for this browser context.
    """
    har_path = str(DIAGNOSTICS_DIR / "session.har") if record_har else ""

    logger.info(f"Launching Chrome (headless={headless}, patchright={USE_PATCHRIGHT})")
    try:
        browser = await playwright.chromium.launch(
            channel="chrome",
            headless=headless,
            # NOTE: do NOT add --disable-web-security or
            # --disable-features=IsolateOrigins,site-per-process here. They break
            # heavy SPAs (LinkedIn's JS bundle fails to hydrate → only the HTML
            # shell renders, with net::ERR_INVALID_ARGUMENT resource errors).
            # Playwright reads cross-origin ATS iframes natively via page.frames /
            # frame.evaluate, so those flags are unnecessary as well as harmful.
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )
    except Exception as exc:
        logger.error(f"Chromium launch failed: {exc}")
        raise

    # IMPORTANT: only set request-agnostic headers here. `extra_http_headers`
    # is applied to EVERY request (documents, XHR, scripts, styles, images), so
    # pinning per-request navigation headers — Sec-Fetch-Dest/Mode/Site,
    # Accept: text/html, Upgrade-Insecure-Requests — corrupts subresource
    # fetches and Chrome rejects them with net::ERR_INVALID_ARGUMENT, leaving
    # SPAs like LinkedIn as an un-hydrated HTML shell. Let Chrome compute those
    # per request; only override Accept-Language.
    context_kwargs: Dict[str, Any] = dict(
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id=BROWSER_TIMEZONE,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
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

    # Inject the pause-widget always; add the navigator spoof only when Patchright
    # is NOT active (Patchright patches those natively — double-patching leaks).
    await context.add_init_script(_WIDGET_JS if USE_PATCHRIGHT else _STEALTH_JS)

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
    # Patchright already applies stealth natively; stacking playwright_stealth on
    # top re-introduces detectable inconsistencies, so skip it in that case.
    if not USE_PATCHRIGHT:
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except ImportError:
            pass
    return page
