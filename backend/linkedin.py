"""
linkedin.py – All LinkedIn-specific automation.

Covers
------
* Manual / automated login with 2FA wait.
* Build a job-search URL from keywords or use a supplied URL.
* Iterate job cards with multiple selector fallbacks.
* Extract job details (title, company, apply URL, apply type).
* Detect whether the Apply button opens Easy-Apply or an external portal.
"""

from __future__ import annotations

import re
import urllib.parse
from urllib.parse import urljoin
from typing import Dict, List, Optional

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from browser import human_delay, save_session
from config import LINKEDIN_EMAIL, LINKEDIN_PASSWORD
from models import JobResult, JobStatus, SearchFilters

# ── Constants ─────────────────────────────────────────────────────────────────

_LOGIN_URL  = "https://www.linkedin.com/login"
_JOBS_BASE  = "https://www.linkedin.com/jobs/search/"
_NAV_TIMEOUT = 30_000   # ms

# Ordered list of CSS selectors tried when locating job cards.
# LinkedIn reshuffles class names regularly; keeping many fallbacks is the
# primary resilience strategy for the discovery step.
_CARD_SELECTORS: List[str] = [
    # 2024-2025 verified selectors (most specific first)
    "li.scaffold-layout__list-item",
    "li[data-occludable-job-id]",
    ".job-card-container",
    ".jobs-search-results__list-item",
    "[data-job-id]",
    ".artdeco-list__item",
]

# Selectors tried when looking for the right-pane job title
_TITLE_SELECTORS: List[str] = [
    ".job-details-jobs-unified-top-card__job-title h1",
    ".jobs-unified-top-card__job-title",
    "h1.t-24",
    "h2.t-24",
    "h1",
]

# Selectors tried for the external Apply button
_APPLY_BTN_SELECTORS: List[str] = [
    # Text-based (most resilient – survives redesigns)
    "a:has-text('Apply on company website')",
    "button:has-text('Apply on company website')",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
    # Attribute-based fallbacks
    ".jobs-apply-button",
    "[data-control-name='jobdetails_topcard_inapply']",
    "[aria-label*='Apply']",
]

_EASY_APPLY_TEXTS = {"easy apply"}
_LINKEDIN_BASE = "https://www.linkedin.com"


# ── Login ─────────────────────────────────────────────────────────────────────

async def ensure_logged_in(
    context: BrowserContext,
    *,
    manual: bool = False,
    linkedin_email: Optional[str] = None,
    linkedin_password: Optional[str] = None,
    emit=None,
) -> bool:
    """
    Verify we have a valid LinkedIn session.
    If not, attempt auto-login (if credentials are configured) or wait for
    the user to log in manually in the visible browser window.

    Returns True on success, False on failure.
    """
    email = linkedin_email or LINKEDIN_EMAIL
    password = linkedin_password or LINKEDIN_PASSWORD
    page = await context.new_page()
    try:
        await page.goto("https://www.linkedin.com/feed", timeout=_NAV_TIMEOUT)
        await human_delay(1.5, 2.5)

        challenge = await detect_access_challenge(page)
        if challenge:
            _emit(emit, "manual_login_required",
                  message="LinkedIn is showing a verification or login wall. Please complete it in the browser.")
            logger.info(f"LinkedIn challenge detected: {challenge}")
            try:
                await page.wait_for_url(
                    re.compile(r"linkedin\.com/(feed|jobs|in/|checkpoint|challenge)"),
                    timeout=180_000,
                )
                await save_session(context)
                return True
            except PWTimeout:
                logger.error("LinkedIn challenge timed out.")
                return False

        # Already logged in?
        if "feed" in page.url or "/in/" in page.url:
            logger.info("LinkedIn session is valid.")
            return True

        # Need to log in
        if email and password:
            return await _auto_login(page, context, linkedin_email=email, linkedin_password=password, emit=emit)

        # No credentials — wait for manual login
        _emit(emit, "manual_login_required",
              message="Please log in to LinkedIn in the browser window. "
                      "The bot will continue automatically after you are logged in.")
        logger.info("Waiting for manual LinkedIn login (up to 3 minutes)…")
        try:
            await page.wait_for_url(
                re.compile(r"linkedin\.com/(feed|jobs|in/)"),
                timeout=180_000,
            )
            await save_session(context)
            return True
        except PWTimeout:
            logger.error("Manual login timed out.")
            return False
    finally:
        await page.close()


async def _auto_login(
    page: Page,
    context: BrowserContext,
    *,
    linkedin_email: str,
    linkedin_password: str,
    emit=None,
) -> bool:
    """Perform automated LinkedIn login using stored credentials."""
    try:
        await page.goto(_LOGIN_URL, timeout=_NAV_TIMEOUT)
        await human_delay()

        await page.fill("#username", linkedin_email)
        await human_delay(0.5, 1.2)
        await page.fill("#password", linkedin_password)
        await human_delay(0.5, 1.0)
        await page.click("[data-litms-control-urn='login-submit'], button[type='submit']")

        # Wait for redirect to feed, jobs, or 2FA
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await human_delay(1.5, 2.5)

        if "challenge" in page.url or "checkpoint" in page.url:
            _emit(emit, "manual_login_required",
                  message="2FA / CAPTCHA required. Please complete it in the browser.")
            logger.info("Waiting for 2FA completion (up to 3 minutes)…")
            await page.wait_for_url(
                re.compile(r"linkedin\.com/(feed|jobs|in/)"),
                timeout=180_000,
            )

        await save_session(context)
        logger.info("Auto-login successful.")
        return True
    except Exception as exc:
        logger.error(f"Auto-login failed: {exc}")
        return False


# ── Job-search URL builder ─────────────────────────────────────────────────────

def build_search_url(
    keywords: str,
    *,
    location: Optional[str] = None,
    filters: Optional[SearchFilters] = None,
) -> str:
    """Convert plain-text keywords into a LinkedIn jobs search URL."""
    params = {
        "keywords": keywords,
        "f_TPR":    f"r{(filters.posted_within_hours if filters else 24) * 3600}",
        "sortBy":   "DD",       # date descending
    }
    if location:
        params["location"] = location
    if filters:
        params.update(filters.extra_params)
    return _JOBS_BASE + "?" + urllib.parse.urlencode(params)


def build_search_urls(keywords: str, filters: Optional[SearchFilters] = None) -> List[str]:
    """Build one or more search URLs, expanding multi-location searches."""
    if filters and filters.locations:
        return [build_search_url(keywords, location=loc, filters=filters) for loc in filters.locations]
    return [build_search_url(keywords, filters=filters)]


async def detect_access_challenge(page: Page) -> Optional[str]:
    """Detect CAPTCHA / checkpoint / auth-wall states on LinkedIn or portals."""
    url = (page.url or "").lower()
    title = ""
    try:
        title = (await page.title()).lower()
    except Exception:
        pass

    markers = ["captcha", "checkpoint", "challenge", "verify you are human", "security verification"]
    if any(m in url or m in title for m in markers):
        return "captcha_required"

    try:
        body = (await page.text_content("body")) or ""
        body = body.lower()
    except Exception:
        body = ""

    if "create account" in body or "sign up" in body or "register" in body:
        return "registration_required"
    if "log in" in body or "sign in" in body or ("username" in body and "password" in body):
        return "login_required"
    if any(m in body for m in markers):
        return "captcha_required"
    return None


# ── Job discovery ─────────────────────────────────────────────────────────────

async def discover_job_cards(page: Page, max_jobs: int) -> List[str]:
    """
    Navigate the search results page and return up to *max_jobs* stable
    job-view URLs.  Tries multiple card selectors for resilience.
    """
    urls: List[str] = []
    selector_used:   Optional[str] = None

    # Find which card selector is present on this page
    for sel in _CARD_SELECTORS:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            selector_used = sel
            logger.info(f"Job cards found via selector: {sel!r}")
            break
        except PWTimeout:
            continue

    if not selector_used:
        logger.warning("No job-card selector matched – page may have changed.")
        return urls

    cards = await page.query_selector_all(selector_used)
    logger.info(f"Found {len(cards)} job cards (capping at {max_jobs}).")

    for card in cards[:max_jobs]:
        # Try to extract a direct job-view URL from the card anchor
        try:
            a = await card.query_selector("a[href*='/jobs/view/']")
            if a:
                href = await a.get_attribute("href")
                if href:
                    clean = urljoin(_LINKEDIN_BASE, href.split("?")[0])
                    if clean not in urls:
                        urls.append(clean)
                    continue
        except Exception:
            pass

        # Fallback: click the card and capture the URL from the right pane
        try:
            await card.scroll_into_view_if_needed()
            await card.click()
            await human_delay(2.0, 3.5)
            if "/jobs/view/" in page.url:
                clean = urljoin(_LINKEDIN_BASE, page.url.split("?")[0])
                if clean not in urls:
                    urls.append(clean)
        except Exception as exc:
            logger.debug(f"Card URL extraction failed: {exc}")

    return urls


# ── Job details ───────────────────────────────────────────────────────────────

async def extract_job_details(page: Page, job_url: str) -> JobResult:
    """
    Navigate to a job-view URL, read title / company, detect the Apply
    button type and resolve the apply URL.
    """
    result = JobResult(job_id=_url_to_id(job_url))
    job_url = urljoin(_LINKEDIN_BASE, job_url)

    try:
        if page.url != job_url:
            await page.goto(job_url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
        await human_delay(2.0, 3.5)

        # ── Title ──────────────────────────────────────────────────────────
        for sel in _TITLE_SELECTORS:
            el = await page.query_selector(sel)
            if el:
                result.title = (await el.inner_text()).strip()
                break

        # ── Company ────────────────────────────────────────────────────────
        for sel in [
            ".job-details-jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name a",
            "[data-test-job-card-company-name]",
        ]:
            el = await page.query_selector(sel)
            if el:
                result.company = (await el.inner_text()).strip()
                break

        # ── Location ───────────────────────────────────────────────────────
        for sel in [
            ".job-details-jobs-unified-top-card__primary-description-container",
            ".jobs-unified-top-card__primary-description",
            ".job-details-jobs-unified-top-card__bullet",
            ".jobs-unified-top-card__bullet",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = " ".join((await el.inner_text()).split())
                if text and len(text) < 120:
                    result.location = text
                    break

        # ── Description ────────────────────────────────────────────────────
        for sel in [
            ".jobs-description-content__text",
            ".jobs-description__content",
            "[data-job-description]",
        ]:
            el = await page.query_selector(sel)
            if el:
                text = " ".join((await el.inner_text()).split())
                if text:
                    result.job_description = text[:6000]
                    break

        # ── Apply button detection ─────────────────────────────────────────
        apply_btn = None
        for sel in _APPLY_BTN_SELECTORS:
            try:
                apply_btn = await page.wait_for_selector(sel, timeout=4_000)
                if apply_btn:
                    break
            except PWTimeout:
                continue

        if apply_btn:
            btn_text = (await apply_btn.inner_text()).strip().lower()
            if any(t in btn_text for t in _EASY_APPLY_TEXTS):
                # Mark Easy Apply jobs as actionable: set them to PENDING and
                # point apply_url at the job page. This allows the orchestrator
                # to attempt an in-site (LinkedIn modal) apply instead of
                # skipping the job outright.
                result.apply_type = "easy_apply"
                result.apply_url = job_url
                result.external_url = None
                result.job_url = job_url
                result.status = JobStatus.PENDING
            else:
                # External portal: resolve the href
                href = await apply_btn.get_attribute("href")
                apply_url = urljoin(_LINKEDIN_BASE, href) if href else job_url
                result.apply_url = apply_url
                result.external_url = apply_url
                result.job_url = job_url
                result.apply_type = "external"
                result.status    = JobStatus.PENDING
        else:
            result.status = JobStatus.SKIPPED
            result.error  = "No Apply button found"
            result.apply_type = "none"

    except Exception as exc:
        logger.error(f"extract_job_details error for {job_url}: {exc}")
        result.status = JobStatus.FAILED
        result.error  = str(exc)

    logger.info(f"[{result.company}] {result.title} → {result.status}")
    return result


# ── Utilities ─────────────────────────────────────────────────────────────────

def _url_to_id(url: str) -> str:
    """Extract numeric job ID from a LinkedIn job-view URL."""
    m = re.search(r"/jobs/view/(\d+)", url)
    return m.group(1) if m else url.rsplit("/", 1)[-1]


def _emit(emit, event_type: str, **kwargs) -> None:
    """Safe fire-and-forget SSE emit (emit may be None in tests)."""
    if emit:
        try:
            emit({"type": event_type, **kwargs})
        except Exception:
            pass
