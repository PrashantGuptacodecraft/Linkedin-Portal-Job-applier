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
import time
import urllib.parse
from urllib.parse import urljoin
from typing import Dict, List, Optional

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

try:
    from .browser import goto_with_retries, human_delay, save_session
    from .config import LINKEDIN_EMAIL, LINKEDIN_PASSWORD
    from .models import JobResult, JobStatus, SearchFilters
except ImportError:
    from browser import goto_with_retries, human_delay, save_session
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
    # Priority order: prefer the semantic jobs-apply-button, then anchors,
    # then data attributes and aria-labels, then text-based fallbacks.
    "button.jobs-apply-button",
    "a.jobs-apply-button",
    "[data-job-apply-source]",
    "button[aria-label*='Apply']",
    "a[aria-label*='Apply']",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "[data-tracking-control-name*='apply']",
]

_EASY_APPLY_TEXTS = {"easy apply"}
_LINKEDIN_BASE = "https://www.linkedin.com"

# Keywords used to prefer portal anchors
_PORTAL_KEYWORDS = ["apply", "career", "careers", "job", "company", "external", "visit"]


def _is_valid_portal_url(href: str) -> bool:
    """Return True if href looks like a usable external portal URL (not an asset or w3.org SVG)."""
    if not href:
        return False
    h = href.strip()
    if h.startswith("javascript:") or h.startswith("mailto:") or h.startswith("data:"):
        return False
    parsed = urllib.parse.urlparse(h)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    netloc = (parsed.netloc or "").lower()
    if "w3.org" in netloc:
        return False
    path = (parsed.path or "").lower()
    if path.endswith(('.svg', '.png', '.jpg', '.jpeg', '.gif', '.css', '.js')):
        return False
    if parsed.scheme in ("http", "https") and netloc:
        return True
    return False


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
        await goto_with_retries(page, "https://www.linkedin.com/feed", timeout=_NAV_TIMEOUT)
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
        await goto_with_retries(page, _LOGIN_URL, timeout=_NAV_TIMEOUT)
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
    """Convert plain-text keywords into a LinkedIn jobs or posts search URL."""
    search_in = filters.extra_params.get('search_in', 'linkedin_jobs') if filters else 'linkedin_jobs'
    
    if search_in == 'linkedin_posts':
        params = {
            "keywords": keywords,
            "sortBy": "\"date_posted\""
        }
        return "https://www.linkedin.com/search/results/content/?" + urllib.parse.urlencode(params)
    else:
        params = {
            "keywords": keywords,
            "f_TPR":    f"r{(filters.posted_within_hours if filters else 24) * 3600}",
            "sortBy":   "DD",       # date descending
        }
        if location:
            params["location"] = location
        if filters:
            clean_extras = {k: v for k, v in filters.extra_params.items() if k != 'search_in'}
            params.update(clean_extras)
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

    selector_matched = False

    # Prefer direct job-view anchors when present; they avoid card clicks.
    direct_anchor_selectors = [
        "a[href*='/jobs/view/']",
        "[data-job-id] a[href*='/jobs/view/']",
        "li[data-occludable-job-id] a[href*='/jobs/view/']",
        "div.job-card-container a[href*='/jobs/view/']",
    ]

    for sel in direct_anchor_selectors + _CARD_SELECTORS:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            selector_matched = True
            logger.info(f"Job cards found via selector: {sel!r}")
            break
        except PWTimeout:
            continue

    if not selector_matched:
        logger.warning("No job-card selector matched – page may have changed.")
        return urls

    # Direct anchors first.
    for sel in direct_anchor_selectors:
        try:
            anchors = await page.query_selector_all(sel)
        except Exception:
            continue
        for anchor in anchors:
            if len(urls) >= max_jobs:
                return urls
            try:
                href = await anchor.get_attribute("href")
                if href:
                    clean = urljoin(_LINKEDIN_BASE, href.split("?")[0])
                    if clean not in urls:
                        urls.append(clean)
            except Exception:
                continue

    if len(urls) >= max_jobs:
        return urls

    # Selector fallback: derive the job URL from card metadata without clicking.
    for sel in [s for s in _CARD_SELECTORS if s != "a[href*='/jobs/view/']"]:
        try:
            cards = await page.query_selector_all(sel)
        except Exception:
            continue
        logger.info(f"Found {len(cards)} job cards via {sel!r} (capping at {max_jobs}).")
        for card in cards:
            if len(urls) >= max_jobs:
                return urls
            try:
                a = await card.query_selector("a[href*='/jobs/view/']")
                if a:
                    href = await a.get_attribute("href")
                    if href:
                        clean = urljoin(_LINKEDIN_BASE, href.split("?")[0])
                        if clean not in urls:
                            urls.append(clean)
                        continue

                job_id = await card.get_attribute("data-job-id")
                if not job_id:
                    job_id = await card.get_attribute("data-occludable-job-id")
                if job_id and job_id.isdigit():
                    clean = f"{_LINKEDIN_BASE}/jobs/view/{job_id}/"
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
            await goto_with_retries(page, job_url, timeout=_NAV_TIMEOUT)
        await human_delay(2.0, 3.5)

        challenge = await detect_access_challenge(page)
        if challenge:
            logger.warning(f"LinkedIn verification wall detected while opening {job_url}: {challenge}")
            recovered = await _wait_for_linkedin_recovery(page, timeout_ms=20_000)
            if not recovered:
                result.status = JobStatus.SKIPPED
                result.error = "captcha_required"
                result.apply_type = "external"
                return result

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
        # Ensure the right-hand details pane is scrolled so that the Apply
        # region is rendered (LinkedIn uses an inner scrollable panel).
        try:
            await page.keyboard.press("Home")
            await human_delay(0.4, 0.6)
            # small downward scroll to reveal buttons
            await page.mouse.wheel(0, 300)
            await human_delay(0.8, 1.0)
        except Exception:
            pass

        apply_btn = None
        try:
            combined_sel = ", ".join(_APPLY_BTN_SELECTORS)
            apply_btn = await page.wait_for_selector(combined_sel, timeout=4_000)
        except PWTimeout:
            pass

        if apply_btn:
            # Clean button text: strip, normalise whitespace, remove non-ASCII
            raw_text = (await apply_btn.inner_text()) or ""
            clean_text = re.sub(r"[^\x20-\x7E]", "", raw_text).strip().lower()
            if any(t in clean_text for t in _EASY_APPLY_TEXTS):
                result.status    = JobStatus.SKIPPED
                result.error     = "Easy Apply – handled separately (not external portal)"
                result.apply_type = "easy_apply"
            else:
                # Detect external icon or explicit page text indicating external apply
                is_external_icon = False
                try:
                    # Look for child svg or i/a icon hints inside the button
                    svg = await apply_btn.query_selector('svg')
                    if svg:
                        cls = (await svg.get_attribute('class') or '') + ' ' + (await svg.get_attribute('data-icon') or '')
                        if any(k in cls.lower() for k in ('external', 'link-external', 'arrow')):
                            is_external_icon = True
                    # also check for anchor inside button with external href
                    a = await apply_btn.query_selector('a')
                    if a:
                        ahref = await a.get_attribute('href') or ''
                        if any(k in ahref.lower() for k in ('external', 'link-external', 'arrow')):
                            is_external_icon = True
                except Exception:
                    pass

                # Also check for explicit text in page indicating an external apply
                external_text_present = False
                try:
                    cnt = await page.locator('text=Responses managed off LinkedIn').count()
                    if cnt and cnt > 0:
                        external_text_present = True
                except Exception:
                    pass

                # Resolve href if present
                href = await apply_btn.get_attribute('href')
                apply_url = urljoin(_LINKEDIN_BASE, href) if href else None

                # If icon or page text indicates external apply, prefer resolving via click
                if (is_external_icon or external_text_present) and not apply_url:
                    logger.info('Apply button appears to be external; resolving via click-time navigation.')
                    apply_url = await _resolve_apply_button_target(page, job_url)

                # If href present and looks like a linkedin safety wrapper, attempt to decode
                if _is_linkedin_safety_url(apply_url):
                    decoded = _decode_linkedin_safety_target(apply_url)
                    if decoded:
                        logger.info(f"Decoded LinkedIn safety URL to external apply URL: {decoded}")
                        apply_url = decoded
                    else:
                        resolved = await _resolve_external_apply_url(page, apply_url)
                        if resolved:
                            logger.info(f"Resolved LinkedIn safety URL to external apply URL: {resolved}")
                            apply_url = resolved

                # If still no apply_url, attempt click-resolution as last resort
                if not apply_url:
                    logger.info(f"Apply button on {job_url} has no href; resolving via click-time navigation.")
                    apply_url = await _resolve_apply_button_target(page, job_url)
                    if _is_linkedin_safety_url(apply_url):
                        decoded = _decode_linkedin_safety_target(apply_url)
                        if decoded:
                            logger.info(f"Decoded LinkedIn safety URL to external apply URL: {decoded}")
                            apply_url = decoded
                        else:
                            resolved = await _resolve_external_apply_url(page, apply_url)
                            if resolved:
                                logger.info(f"Resolved LinkedIn safety URL to external apply URL: {resolved}")
                                apply_url = resolved

                if not apply_url:
                    result.status = JobStatus.SKIPPED
                    result.error = "Could not resolve external apply URL from LinkedIn Apply button"
                    result.apply_type = "external"
                    logger.info(f"[{result.company}] {result.title} → {result.status}")
                    return result

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


async def _wait_for_linkedin_recovery(page: Page, *, timeout_ms: int = 180_000) -> bool:
    """Wait for a manually solved verification wall to clear."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    warned = False
    while time.monotonic() < deadline:
        challenge = await detect_access_challenge(page)
        url = (page.url or "").lower()
        if not challenge and "linkedin.com" in url and not any(marker in url for marker in ("checkpoint", "challenge", "authwall", "safety/go", "/safety/")):
            return True
        if not warned:
            logger.warning("LinkedIn is showing a verification wall. Solve it manually in the browser window; automation will wait up to 3 minutes.")
            warned = True
        await human_delay(2.5, 5.0)
    return False


def _is_linkedin_safety_url(url: str) -> bool:
    url_l = (url or "").lower()
    return "linkedin.com" in url_l and ("/safety/" in url_l or "safety/go" in url_l or "checkpoint" in url_l)


def _decode_linkedin_safety_target(safety_url: str) -> Optional[str]:
    """If safety/go contains a `url=` parameter, decode and return it when it looks valid.

    This avoids opening the LinkedIn interstitial when the real ATS URL is exposed
    as a query parameter.
    """
    if not safety_url:
        return None
    try:
        parsed = urllib.parse.urlparse(safety_url)
        q = urllib.parse.parse_qs(parsed.query)
        vals = q.get("url") or q.get("target") or q.get("u")
        if not vals:
            return None
        candidate = vals[0]
        # Some LinkedIn wrappers double-encode; attempt an extra unquote if needed.
        candidate = urllib.parse.unquote(candidate)
        candidate = candidate.strip()
        if _is_valid_portal_url(candidate) and 'linkedin.com' not in candidate.lower():
            return candidate
    except Exception:
        pass
    return None


async def _resolve_external_apply_url(page: Page, safety_url: str) -> Optional[str]:
    """Open a LinkedIn safety/interstitial page and extract the real external URL."""
    temp = None
    try:
        temp = await page.context.new_page()
        await goto_with_retries(temp, safety_url, timeout=_NAV_TIMEOUT)
        await human_delay(1.0, 2.0)

        anchors = await temp.query_selector_all('a[href]')
        candidates: list[str] = []
        for anchor in anchors:
            try:
                href = (await anchor.get_attribute('href') or '').strip()
                if not href or href.startswith('#'):
                    continue
                if href.startswith('/'):
                    href = urljoin(_LINKEDIN_BASE, href)
                if 'linkedin.com' in (href or '').lower():
                    continue
                if not _is_valid_portal_url(href):
                    continue
                text = ((await anchor.inner_text()) or "").lower()
                aria = (await anchor.get_attribute('aria-label') or "").lower()
                if any(k in text or k in aria for k in _PORTAL_KEYWORDS):
                    return href
                candidates.append(href)
            except Exception:
                continue
        if candidates:
            return candidates[0]

        try:
            body = await temp.content()
            match = re.search(r'https?://[^"\'\s>]+', body, re.I)
            if match:
                href = match.group(0)
                if 'linkedin.com' not in href.lower() and _is_valid_portal_url(href):
                    return href
        except Exception:
            pass

        if temp.url and 'linkedin.com' not in temp.url.lower() and _is_valid_portal_url(temp.url):
            logger.info(f"LinkedIn safety page navigated directly to external URL: {temp.url}")
            return temp.url
    except Exception as exc:
        logger.debug(f"Failed to resolve LinkedIn safety URL {safety_url}: {exc}")
    finally:
        if temp:
            try:
                await temp.close()
            except Exception:
                pass
    return None


async def _resolve_apply_button_target(page: Page, job_url: str) -> Optional[str]:
    """Click the LinkedIn Apply button on a temporary page and capture the target URL."""
    temp = None
    try:
        temp = await page.context.new_page()
        try:
            from playwright_stealth import stealth_async
            await stealth_async(temp)
        except ImportError:
            pass
        await goto_with_retries(temp, job_url, timeout=_NAV_TIMEOUT)
        await human_delay(1.5, 2.5)

        btn = None
        try:
            combined_sel = ", ".join(_APPLY_BTN_SELECTORS)
            btn = await temp.wait_for_selector(combined_sel, timeout=4_000)
        except PWTimeout:
            pass

        if not btn:
            return None

        try:
            async with temp.expect_popup(timeout=6_000) as popup_info:
                await btn.click(force=True, timeout=6_000)
            popup = await popup_info.value
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            popup_url = popup.url
            if popup_url:
                # Prefer decoded target if safety wrapper includes it
                if _is_linkedin_safety_url(popup_url):
                    decoded = _decode_linkedin_safety_target(popup_url)
                    if decoded:
                        return decoded
                    return popup_url
                if _is_valid_portal_url(popup_url):
                    return popup_url
        except PWTimeout:
            try:
                await btn.click(force=True, timeout=6_000)
            except Exception:
                pass
        except Exception as exc:
            logger.debug(f"LinkedIn Apply popup resolution failed for {job_url}: {exc}")

        await human_delay(1.0, 2.0)
        if temp.url and temp.url != job_url:
            if _is_linkedin_safety_url(temp.url):
                decoded = _decode_linkedin_safety_target(temp.url)
                if decoded:
                    return decoded
                return temp.url
            if _is_valid_portal_url(temp.url):
                return temp.url

        anchors = await temp.query_selector_all('a[href]')
        candidates = []
        for anchor in anchors:
            try:
                href = (await anchor.get_attribute('href') or '').strip()
                if not href or href.startswith('#'):
                    continue
                if href.startswith('/'):
                    href = urljoin(_LINKEDIN_BASE, href)
                if 'linkedin.com' in (href or '').lower():
                    continue
                if not _is_valid_portal_url(href):
                    continue
                text = ((await anchor.inner_text()) or "").lower()
                aria = (await anchor.get_attribute('aria-label') or "").lower()
                if any(k in text or k in aria for k in _PORTAL_KEYWORDS):
                    return href
                candidates.append(href)
            except Exception:
                continue
        if candidates:
            return candidates[0]

        try:
            body = await temp.content()
            match = re.search(r'https?://[^"\'\s>]+', body, re.I)
            if match:
                href = match.group(0)
                if 'linkedin.com' not in href.lower() and _is_valid_portal_url(href):
                    return href
        except Exception:
            pass

    except Exception as exc:
        logger.debug(f"Failed to resolve LinkedIn Apply button target for {job_url}: {exc}")
    finally:
        if temp:
            try:
                await temp.close()
            except Exception:
                pass
    return None


async def open_external_apply_page(context, job_url: str, page: Page) -> tuple[Optional[Page], Optional[str]]:
    """Attempt to click the LinkedIn Apply button and capture the external portal page.

    Returns (portal_page, portal_url). If a new tab opened, portal_page is that
    Page object. If navigation happened in the same page, portal_page is the
    provided page object. If nothing could be captured, both values may be None.
    """
    try:
        # Ensure we're on the job URL
        if page.url != job_url:
            try:
                await goto_with_retries(page, job_url, timeout=_NAV_TIMEOUT)
            except Exception:
                pass
        await human_delay(1.0, 2.0)

        # Scroll into view like extract_job_details does
        try:
            await page.keyboard.press("Home")
            await human_delay(0.3, 0.6)
            await page.mouse.wheel(0, 300)
            await human_delay(0.6, 1.0)
        except Exception:
            pass

        btn = None
        try:
            combined_sel = ", ".join(_APPLY_BTN_SELECTORS)
            btn = await page.query_selector(combined_sel)
        except Exception:
            pass

        if not btn:
            return None, None

        # Try clicking and wait for a popup (new tab)
        try:
            popup_task = asyncio.create_task(context.wait_for_event('page', timeout=15_000))
            try:
                await btn.click(force=True, timeout=6_000)
            except Exception:
                # If direct click fails, try clicking by coordinates
                try:
                    box = await btn.bounding_box()
                    if box:
                        x = box['x'] + box['width'] / 2
                        y = box['y'] + box['height'] / 2
                        await page.mouse.click(x, y)
                    else:
                        raise
                except Exception:
                    popup_task.cancel()
                    raise

            # Wait a moment for popup or same-page navigation
            try:
                popup = await popup_task
                try:
                    await popup.wait_for_load_state('domcontentloaded', timeout=12_000)
                except Exception:
                    pass
                # If popup navigated to external site, return it
                if popup.url and 'linkedin.com' not in popup.url.lower():
                    return popup, popup.url
                return popup, popup.url
            except Exception:
                # No popup — maybe navigation occurred in same page
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=6_000)
                except Exception:
                    pass
                if page.url and 'linkedin.com' not in page.url.lower():
                    return page, page.url
        except Exception as exc:
            # As a last resort, try bounding box click and then check page/url
            try:
                box = await btn.bounding_box()
                if box:
                    x = box['x'] + box['width'] / 2
                    y = box['y'] + box['height'] / 2
                    await page.mouse.click(x, y)
                    await human_delay(1.0, 2.0)
                    # Try to detect popup again
                    try:
                        popup = await context.wait_for_event('page', timeout=6_000)
                        try:
                            await popup.wait_for_load_state('domcontentloaded', timeout=10_000)
                        except Exception:
                            pass
                        if popup.url and 'linkedin.com' not in popup.url.lower():
                            return popup, popup.url
                    except Exception:
                        pass
                    if page.url and 'linkedin.com' not in page.url.lower():
                        return page, page.url
            except Exception:
                logger.debug(f"open_external_apply_page final click attempts failed for {job_url}: {exc}")
                return None, None

    except Exception as exc:
        logger.debug(f"open_external_apply_page error for {job_url}: {exc}")
    return None, None

async def discover_post_leads(page, max_leads: int) -> list:
    """Scroll through LinkedIn posts and extract recruiter emails."""
    from .browser import human_delay
    import re
    from loguru import logger
    
    leads = []
    seen_emails = set()
    scroll_attempts = 0
    
    while len(leads) < max_leads and scroll_attempts < 10:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(2.0, 3.5)
        
        try:
            see_mores = await page.query_selector_all("button.see-more-text")
            for btn in see_mores:
                if await btn.is_visible():
                    await btn.click()
                    await human_delay(0.2, 0.5)
        except Exception:
            pass
            
        posts = await page.query_selector_all("div.feed-shared-update-v2")
        for post in posts:
            if len(leads) >= max_leads:
                break
                
            try:
                text_el = await post.query_selector("div.update-components-text")
                if not text_el: continue
                
                text = await text_el.inner_text()
                
                emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
                if emails:
                    email = emails[0].lower()
                    if email not in seen_emails:
                        seen_emails.add(email)
                        
                        author = "Recruiter"
                        author_el = await post.query_selector("span.update-components-actor__title")
                        if author_el:
                            author_text = await author_el.inner_text()
                            author = author_text.split('\n')[0].strip()
                            
                        leads.append({
                            "email": email,
                            "author": author,
                            "text": text,
                            "url": page.url
                        })
            except Exception as e:
                logger.debug(f"Error parsing post: {e}")
                
        scroll_attempts += 1
        
    return leads
