"""
portal.py – External portal autofill engine.

Strategy (layered, most-specific to most-generic)
-------------------------------------------------
1. Known-portal adapter  – hand-crafted, tested selectors for Workday /
   Greenhouse / Lever / Taleo / iCIMS / SAP SuccessFactors.
2. Semantic-selector fill – locate fields by aria-label / placeholder / name
   attribute matching candidate profile keys.
3. Heuristic fallback     – iterate all visible inputs and guess intent from
   surrounding label text.
4. File upload            – detect any <input type="file"> and attach resume.

Each layer only handles the fields the previous layer missed, so results
accumulate across layers.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

async def _call_gemini_with_retry(client, prompt: str, retries: int = 4) -> Any:
    """Call Gemini, rotating through every configured API key on quota/rate-limit
    (429) before falling back to a timed wait. Also retries transient 503s.

    Add more keys by setting GEMINI_API_KEY to a comma-separated list in .env."""
    from .config import rotate_gemini_key, get_current_gemini_key, GEMINI_API_KEYS, GEMINI_MODEL
    import google.generativeai as genai

    n_keys = max(1, len(GEMINI_API_KEYS))
    # Enough attempts to try every key at least once, plus a couple of timed waits.
    max_attempts = max(retries, n_keys + 2)
    keys_tried_this_round = 1  # the current key counts as already tried

    for attempt in range(max_attempts):
        try:
            if hasattr(client, "generate_content_async"):
                return await client.generate_content_async(prompt)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, client.generate_content, prompt)
        except Exception as e:
            err_str = str(e)
            err_low = err_str.lower()
            if attempt >= max_attempts - 1:
                logger.error(f"Gemini API failed after {attempt + 1} attempts: {err_str[:120]}")
                raise e

            is_quota = ("429" in err_str or "quota" in err_low
                        or "rate limit" in err_low or "resource_exhausted" in err_low)
            is_overload = ("503" in err_str or "overloaded" in err_low or "unavailable" in err_low)

            # Rotate to the next key and retry immediately while keys remain untried.
            if is_quota and n_keys > 1 and keys_tried_this_round < n_keys:
                new_key = rotate_gemini_key()
                keys_tried_this_round += 1
                logger.warning(
                    f"Gemini quota hit — rotating to key {new_key[:8]}… "
                    f"({keys_tried_this_round}/{n_keys}) and retrying immediately."
                )
                try:
                    genai.configure(api_key=new_key)
                    client = genai.GenerativeModel(GEMINI_MODEL)
                except Exception:
                    pass
                continue

            # All keys exhausted this round (or single key / transient error): wait.
            wait_time = 5.0
            if is_quota:
                wait_time = 40.0
                m = re.search(r"retry in ([0-9.]+)s", err_str)
                if m:
                    wait_time = float(m.group(1)) + 1.0
            elif is_overload:
                wait_time = 10.0
            logger.warning(
                f"Gemini error: {err_str[:100]}… waiting {wait_time:.1f}s "
                f"(attempt {attempt + 1}/{max_attempts})."
            )
            await asyncio.sleep(wait_time)
            # Start a fresh rotation round from the current key after waiting.
            keys_tried_this_round = 1
            try:
                genai.configure(api_key=get_current_gemini_key())
                client = genai.GenerativeModel(GEMINI_MODEL)
            except Exception:
                pass


from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

try:
    from .browser import goto_with_retries, human_delay
    from .models import CandidateProfile
except ImportError:
    from browser import goto_with_retries, human_delay
    from models import CandidateProfile


# ── Pause & Resume System ─────────────────────────────────────────────────────

_pause_events: Dict[str, asyncio.Event] = {}
_manual_modes: Dict[str, bool] = {}
_pause_timestamps: Dict[str, float] = {}

def get_pause_status(session_id: str) -> Dict[str, Any]:
    is_paused = False
    if session_id in _pause_events and not _pause_events[session_id].is_set():
        is_paused = True
    
    pause_time = _pause_timestamps.get(session_id, 0)
    duration = time.time() - pause_time if is_paused and pause_time else 0
    return {
        "paused": is_paused,
        "pause_duration_seconds": round(duration, 1)
    }

def trigger_pause_external(session_id: str):
    if session_id not in _pause_events:
        _pause_events[session_id] = asyncio.Event()
        _pause_events[session_id].set()
    if _pause_events[session_id].is_set():
        _pause_events[session_id].clear()
        _pause_timestamps[session_id] = time.time()

def trigger_resume_external(session_id: str):
    if session_id in _pause_events:
        _pause_events[session_id].set()
        if session_id in _pause_timestamps:
            del _pause_timestamps[session_id]

async def pause_checkpoint(session_id: str, emit: Any, reason: str, page: Any = None, client: Any = None):
    if session_id not in _pause_events:
        _pause_events[session_id] = asyncio.Event()
        _pause_events[session_id].set()  # running by default

    if not _pause_events[session_id].is_set():
        # Just update timestamp if already paused by external? Wait, no, if it's paused externally it doesn't emit takeover.
        pass
    else:
        # Pause triggered from code
        _pause_events[session_id].clear()
        _pause_timestamps[session_id] = time.time()
        if emit:
            emit({"type": "manual_takeover", "reason": reason})

    if page:
        try:
            def _resume():
                trigger_resume_external(session_id)
            await page.expose_function("antigravityResumeBot", _resume)
        except Exception:
            pass # Already exposed

        try:
            # We must escape single quotes in reason
            safe_reason = reason.replace("'", "\\'") if reason else "Bot Paused - Manual Action Required"
            await page.evaluate(f"() => {{ if (window.showAntigravityPauseUI) window.showAntigravityPauseUI('{safe_reason}'); }}")
        except Exception as e:
            logger.warning(f"Could not inject pause UI: {e}")

    await _pause_events[session_id].wait()
    # Resume triggered
    
    if page:
        try:
            await page.evaluate("""
            () => {
                if (window.hideAntigravityPauseUI) window.hideAntigravityPauseUI();
            }
            """)
        except Exception:
            pass

    if emit:
        emit({"type": "bot_resumed", "message": "AI resuming control in 3 seconds..."})
    await asyncio.sleep(3)
    
    if page:
        import os
        # Take a fresh screenshot to project directory on resume
        os.makedirs("screenshots", exist_ok=True)
        screenshot_path = f"screenshots/resume_{session_id}_{int(time.time())}.png"
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            if emit:
                emit({"type": "log", "level": "info", "message": f"Resume screenshot saved: {screenshot_path}"})
        except Exception:
            pass
            
        if client:
            try:
                # Need to re-run classify_portal_page but it is below. We can import or call it.
                # Actually, classify_portal_page might be used within the logic that called pause_checkpoint,
                # but we were asked to "re-run classify_portal_page before continuing". 
                # Since classify_portal_page just returns page_type and reasoning, 
                # re-running it here doesn't change the execution flow unless we return it.
                # Just calling it to update the internal state or logs:
                global classify_portal_page
                if "classify_portal_page" in globals():
                    await globals()["classify_portal_page"](page, client)
            except Exception:
                pass


async def check_pause_state(session_id: str):
    if session_id in _pause_events and not _pause_events[session_id].is_set():
        await _pause_events[session_id].wait()

# ── Portal fingerprint registry ───────────────────────────────────────────────
# Each entry: (url-pattern-regex, adapter-function)
# Adapters are defined further below and registered at module load time.
_PORTAL_ADAPTERS: List[Tuple[re.Pattern, Any]] = []

def _register(pattern: str):
    """Decorator: register a portal adapter for URLs matching *pattern*."""
    def decorator(fn):
        _PORTAL_ADAPTERS.append((re.compile(pattern, re.I), fn))
        return fn
    return decorator


# ── Public entry-point ────────────────────────────────────────────────────────

async def autofill_portal_form(
    context: BrowserContext,
    apply_target: Any,
    candidate: CandidateProfile,
    login_credentials: Optional[Any] = None,
    emit: Optional[Any] = None,
    session_id: str = "default",
) -> Tuple[Page, int, Optional[str]]:
    """
    Open *apply_target* (URL or Page), autofill the application form, and
    attempt to upload the resume.
    """
    import urllib.parse
    from .config import SKIP_DOMAINS, DomainSkippedError
    try:
        from playwright_stealth import stealth_async
    except ImportError:
        stealth_async = None

    # Check SKIP_DOMAINS
    target_url = getattr(apply_target, 'url', str(apply_target))
    try:
        domain = urllib.parse.urlparse(target_url).netloc
        if not domain:
            domain = target_url
    except Exception:
        domain = ""
    for skip_d in SKIP_DOMAINS:
        if skip_d in domain:
            raise DomainSkippedError("Domain in skip list — Cloudflare protected")

    page = None
    created_new_page = False
    try:
        if hasattr(apply_target, 'url') and getattr(apply_target, 'goto', None):
            page = apply_target
        else:
            page = await context.new_page()
            created_new_page = True
            if stealth_async:
                await stealth_async(page)
            try:
                await goto_with_retries(page, str(apply_target), timeout=35_000)
            except Exception:
                pass
        await human_delay(2.5, 4.0)
        apply_url = str(page.url or '')
        url_l = (page.url or "").lower()
        if "linkedin.com" in url_l and ("/safety/" in url_l or "safety/go" in url_l or "checkpoint" in url_l):
            try:
                followed = await _follow_linkedin_interstitial(page)
                if followed:
                    logger.info(f"Followed LinkedIn interstitial to external URL: {page.url}")
            except Exception as exc:
                logger.debug(f"Error following LinkedIn interstitial: {exc}")
                
        # ── Cloudflare Detect & Wait ─────────────────────────────────────────
        try:
            curr_title = (await page.title()).lower()
            curr_url = (page.url or "").lower()
            curr_html = (await page.content()).lower()
            if ("just a moment" in curr_title or 
                "verifying you are human" in curr_title or 
                "challenge" in curr_url or 
                "cf-turnstile" in curr_html or 
                "ray id" in curr_html):
                
                logger.info("Cloudflare detected — waiting for auto-resolution")
                if emit:
                    emit({"type": "log", "level": "info", "message": "Cloudflare detected — waiting for auto-resolution (up to 30s)..."})
                
                try:
                    await page.wait_for_function("document.title !== 'Just a moment...'", timeout=30000)
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                
                # Check if still blocked
                curr_title = (await page.title()).lower()
                curr_html = (await page.content()).lower()
                if "just a moment" in curr_title or "verifying you are human" in curr_title or "cf-turnstile" in curr_html:
                    logger.info("Cloudflare did not auto-resolve. Attempting to click Turnstile iframe.")
                    try:
                        # Attempt to find Turnstile iframe and click center
                        iframe_el = await page.query_selector("iframe[src*='challenges.cloudflare.com']")
                        if not iframe_el:
                            iframe_el = await page.query_selector("iframe[title*='widget']")
                        if iframe_el:
                            box = await iframe_el.bounding_box()
                            if box:
                                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                await human_delay(4.0, 6.0)
                    except Exception as e:
                        logger.debug(f"Failed to click turnstile: {e}")
                        
                # Final check after click
                curr_title = (await page.title()).lower()
                curr_html = (await page.content()).lower()
                if "just a moment" in curr_title or "verifying you are human" in curr_title or "cf-turnstile" in curr_html:
                    logger.info("Cloudflare still blocking. Triggering pause checkpoint.")
                    await pause_checkpoint(session_id, emit, "Cloudflare verification required — please solve manually, then resume.", page=page)
                    
                    # Optional: wait slightly for page load after resume
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"CF detect error: {e}")
    except PWTimeout:
        logger.warning(f"Portal page load timed out: {getattr(page,'url', str(apply_target))}")
    except Exception as exc:
        logger.warning(f"Portal page load failed after retries for {getattr(page,'url', str(apply_target))}: {exc}")

    portal_state = await detect_portal_state(page)
    if portal_state:
        logger.info(f"Portal state detected: {portal_state}")

    filled = 0
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
        await human_delay(0.8, 1.5)
        await page.evaluate("window.scrollTo(0, 0)")
        await human_delay(0.5, 1.0)
    except Exception:
        pass

    filled += await _discover_and_click_apply(page)

    adapter_matched = False
    for pattern, adapter in _PORTAL_ADAPTERS:
        try:
            page_url = page.url or ''
        except Exception:
            page_url = ''
        if pattern.search(str(apply_url or '')) or pattern.search(page_url):
            logger.info(f"Using portal adapter: {adapter.__name__}")
            filled += await adapter(page, candidate)
            adapter_matched = True
            break

    if not adapter_matched:
        # ── Layer 2: Semantic fill (reliable, no AI needed) ───────────────
        logger.info("No static adapter matched. Running semantic fill layers first...")
        if emit:
            emit({"type": "log", "level": "info", "message": "Running semantic form fill..."})
        semantic_filled = await _semantic_fill(page, candidate)
        filled += semantic_filled
        logger.info(f"Semantic fill: {semantic_filled} field(s)")

        # ── Layer 2b: React phone input ───────────────────────────────────
        try:
            phone_filled = await _fill_react_phone_input(page, candidate)
            filled += phone_filled
        except Exception as exc:
            logger.debug(f"React phone fill error: {exc}")

        # ── Layer 3: Ant Design custom selects ────────────────────────────
        try:
            ant_filled = await _fill_ant_design_selects(page, candidate)
            filled += ant_filled
        except Exception as exc:
            logger.debug(f"Ant Design select fill error: {exc}")

        # ── Layer 4: Native dropdown selects ──────────────────────────────
        try:
            dd_filled = await _fill_dropdowns(page, candidate)
            filled += dd_filled
        except Exception as exc:
            logger.debug(f"Dropdown fill error: {exc}")

        # ── Layer 5: Resume upload ────────────────────────────────────────
        if candidate.resume_path:
            try:
                upload_filled = await _upload_resume_basic(page, candidate.resume_path)
                filled += upload_filled
            except Exception as exc:
                logger.debug(f"Resume upload error: {exc}")

        # ── Layer 6: Dispatch React events to register values ─────────────
        try:
            await _dispatch_react_events(page)
        except Exception as exc:
            logger.debug(f"React event dispatch error: {exc}")

        # ── Layer 7: AI fallback (for radio buttons, dropdowns, and form submission)
        logger.info(f"Semantic fill complete. Handing over to AI Handler for remaining fields (radio/checkbox) and submission...")
        if emit:
            emit({"type": "log", "level": "info", "message": "Initializing AI-powered application flow to finish remaining fields and submit..."})
        try:
            ai_filled = await _ai_fallback_handler(page, candidate, login_credentials, emit, session_id)
            filled += ai_filled
        except Exception as exc:
            logger.error(f"AI fallback handler crashed: {exc}")
            if emit:
                emit({"type": "log", "level": "warning", "message": f"AI handler error: {exc}"})

    logger.info(f"Portal autofill complete – {filled} field(s) filled.")
    return page, filled, portal_state

async def _discover_and_click_apply(page) -> int:
    """Look for 'Apply', 'Apply Now' buttons and click them to open the form.
    IMPORTANT: Do NOT click 'Submit', 'Send application', or form submit buttons."""
    # Only look for buttons that OPEN the application form, not submit it
    apply_selectors = [
        "button:has-text('Apply Now')",
        "a:has-text('Apply Now')",
        "button:has-text('Apply for this job')",
        "a:has-text('Apply for this job')",
        "button:has-text('Start Application')",
        "a:has-text('Start Application')",
        "[data-automation-id='applyButton']",
        "button.apply-button",
        "a.apply-button",
        "#apply-button",
        ".apply-btn",
    ]
    # Words that indicate a SUBMIT action (not an open-form action)
    submit_words = [
        "applied", "close", "cancel", "submit", "send",
        "confirm", "done", "finish", "complete", "save",
    ]
    for sel in apply_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                btn_text = (await btn.inner_text()).strip().lower()
                # Skip if the button text contains submit-like words
                if any(w in btn_text for w in submit_words):
                    logger.debug(f"Skipping submit-like button: '{btn_text}'")
                    continue
                # Skip if form fields are already visible (form is already open)
                form_fields = await page.query_selector_all(
                    "input[type='text']:visible, input[type='email']:visible, "
                    "input[type='tel']:visible, textarea:visible"
                )
                visible_count = 0
                for f in form_fields[:5]:
                    try:
                        if await f.is_visible():
                            visible_count += 1
                    except Exception:
                        pass
                if visible_count >= 2:
                    logger.debug(f"Form fields already visible ({visible_count}), skipping apply button click")
                    return 0
                await btn.click()
                await human_delay(1.5, 3.0)
                logger.info(f"Clicked apply button: {sel}")
                return 0
        except Exception:
            continue
    return 0


async def _follow_linkedin_interstitial(page: Page) -> bool:
    """
    On LinkedIn safety/interstitial pages (e.g. /safety/go), there's typically
    an anchor that points to the actual external site. Try to locate a
    non-LinkedIn href on the page and navigate there. Returns True if a
    follow/navigation occurred.
    """
    try:
        from .linkedin import _is_valid_portal_url
    except ImportError:
        def _is_valid_portal_url(h: str) -> bool: return True

    try:
        for anchor in await page.query_selector_all('a[href]'):
            try:
                href = (await anchor.get_attribute('href') or '').strip()
                if not href or href.startswith('javascript:') or href.startswith('#'):
                    continue
                if href.startswith('/'):
                    base = page.url.split('?')[0].split('#')[0]
                    href = '/'.join(base.split('/')[:3]) + href
                if not _is_valid_portal_url(href) or 'linkedin.com' in href.lower():
                    continue

                popup_task = asyncio.create_task(page.context.wait_for_event('page', timeout=3000))
                try:
                    await anchor.click()
                except Exception:
                    popup_task.cancel()
                    raise

                await human_delay(1.0, 2.0)

                try:
                    popup = await popup_task
                    await popup.wait_for_load_state('domcontentloaded', timeout=12_000)
                    if popup.url and 'linkedin.com' not in popup.url.lower():
                        return True
                except Exception:
                    pass

                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=12_000)
                except Exception:
                    pass

                if href.lower() in (page.url or '').lower() or 'linkedin.com' not in (page.url or '').lower():
                    return True

                await goto_with_retries(page, href, timeout=25_000)
                await human_delay(1.0, 2.0)
                return True
            except Exception:
                continue

        meta = await page.query_selector('meta[http-equiv]')
        if meta:
            content = (await meta.get_attribute('content') or '')
            match = re.search(r"url=(https?://[^;]+)", content, re.I)
            if match:
                href = match.group(1)
                if 'linkedin.com' not in href.lower() and _is_valid_portal_url(href):
                    await goto_with_retries(page, href, timeout=25_000)
                    await human_delay(1.0, 2.0)
                    return True
    except Exception as exc:
        logger.debug(f"_follow_linkedin_interstitial error: {exc}")
    return False


async def detect_portal_state(page: Page) -> Optional[str]:
    """Detect login / registration / CAPTCHA walls on the application portal.

    Only flags a wall when the *prominent* page content (URL, title, headings)
    indicates a blocking state.  If application-form inputs (name / email /
    phone) are already present on the page, we assume the form is usable and
    skip the login/registration check — those strings likely just appear in a
    header or footer nav link.
    """
    url = (page.url or "").lower()
    try:
        title = (await page.title()).lower()
    except Exception:
        title = ""

    # CAPTCHA / challenge detection (always check — these block everything)
    captcha_markers = [
        "captcha", "checkpoint", "challenge", "verify you are human",
        "security verification", "just a moment", "checking your browser",
        "cloudflare", "please stand by", "robot"
    ]
    # Treat known LinkedIn safety/interstitial pages as captcha/challenge walls
    if ("linkedin.com" in url and ("/safety/" in url or "safety/go" in url or "checkpoint" in url)):
        return "captcha_required"

    if any(marker in url or marker in title for marker in captcha_markers):
        return "captcha_required"

    # If application-form fields are already visible, prefer the form over
    # generic challenge strings that may appear in hidden markup or scripts.
    try:
        form_fields = await page.query_selector_all(
            "input[type='text'], input[type='email'], input[type='tel'], textarea"
        )
        visible_fields = 0
        for f in form_fields[:10]:
            try:
                if await f.is_visible():
                    visible_fields += 1
            except Exception:
                pass
        if visible_fields >= 2:
            return None  # looks like a real form — not a login wall
    except Exception:
        pass

    # Explicit CAPTCHA widgets / providers are much stronger signals than
    # just the presence of the word "captcha" somewhere in the body HTML.
    try:
        captcha_widgets = await page.query_selector_all(
            "iframe[src*='captcha'], iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
            "div.g-recaptcha, div.h-captcha, [data-sitekey], [class*='captcha' i]"
        )
        for widget in captcha_widgets:
            try:
                if await widget.is_visible():
                    return "captcha_required"
            except Exception:
                continue
    except Exception:
        pass

    # Check only prominent elements for login / registration walls
    prominent_text = ""
    for sel in ["h1", "h2", "h3", "main", "[role='main']", "form", ".content", "#content"]:
        try:
            el = await page.query_selector(sel)
            if el:
                txt = await el.inner_text()
                prominent_text += " " + txt.lower()
        except Exception:
            pass

    # Registration / login detection on prominent content only
    if "create account" in prominent_text or "sign up" in prominent_text:
        return "registration_required"
    if "log in" in prominent_text or "sign in" in prominent_text:
        # Only flag if the prominent area has a login form, not just a nav link
        if "password" in prominent_text or "username" in prominent_text:
            return "login_required"

    return None


# ── Field-value mapping ───────────────────────────────────────────────────────

def _profile_as_field_map(c: CandidateProfile) -> Dict[str, str]:
    """
    Return a flat dict mapping canonical field names to candidate values.
    Keys are deliberately broad so they match many real label/name variations.
    """
    name_parts = c.name.strip().split(" ", 1)
    location_parts = (c.location or "").split(",")
    city = location_parts[0].strip() if location_parts else ""
    state_country = location_parts[1].strip() if len(location_parts) > 1 else ""

    return {
        # full name variants
        "full name":    c.name,
        "full_name":    c.name,
        "name":         c.name,
        "candidate name": c.name,
        "applicant name": c.name,
        # first / last name (including Ant Design dot-notation IDs)
        "first name":   name_parts[0],
        "first_name":   name_parts[0],
        "firstname":    name_parts[0],
        "given name":   name_parts[0],
        "given_name":   name_parts[0],
        "name first name": name_parts[0],    # matches "name.first_name" after normalization
        "name first":   name_parts[0],
        "last name":    name_parts[1] if len(name_parts) > 1 else "",
        "last_name":    name_parts[1] if len(name_parts) > 1 else "",
        "lastname":     name_parts[1] if len(name_parts) > 1 else "",
        "surname":      name_parts[1] if len(name_parts) > 1 else "",
        "family name":  name_parts[1] if len(name_parts) > 1 else "",
        "family_name":  name_parts[1] if len(name_parts) > 1 else "",
        "name last name": name_parts[1] if len(name_parts) > 1 else "",  # matches "name.last_name"
        "name last":    name_parts[1] if len(name_parts) > 1 else "",
        # contact
        "email":        c.email,
        "email address": c.email,
        "e mail":       c.email,
        "emailaddress": c.email,
        "contact email": c.email,
        "your contact email": c.email,      # matches E-Logic placeholder
        "phone":        c.phone,
        "phone number": c.phone,
        "telephone":    c.phone,
        "tel":          c.phone,
        "mobile":       c.phone,
        "mobile number": c.phone,
        "cell phone":   c.phone,
        "contact number": c.phone,
        # location / address (including Ant Design form IDs)
        "location":     c.location or "",
        "city":         city,
        "town":         city,
        "candidate city": city,               # matches "address.candidate_city"
        "address candidate city": city,
        "enter city":   city,                 # matches placeholder
        "state":        state_country,
        "province":     state_country,
        "candidate state": state_country,     # matches "address.candidate_state"
        "address candidate state": state_country,
        "country":      state_country,
        "candidate country": state_country,   # matches "address.candidate_country"
        "address candidate country": state_country,
        "address":      c.location or "",
        "street":       "",                   # need to be filled via AI or left empty
        "candidate address1": "",
        "address candidate address1": "",
        "enter street": "",
        "postal code":  "",
        "candidate postal code": "",
        "address candidate postal code": "",
        "enter postal code": "",
        "zip":          "",
        "zip code":     "",
        "zipcode":      "",
        # cover letter
        "cover letter": c.cover_text,
        "cover_letter": c.cover_text,
        "coverletter":  c.cover_text,
        "message":      c.cover_text,
        "additional information": c.cover_text,
        "comments":     c.cover_text,
        "notes":        c.cover_text,
        "why are you interested": c.cover_text,
        # generated-resume fields
        "skills":       getattr(c, "technical_skills", "") or "",
        "technical skills": getattr(c, "technical_skills", "") or "",
        "projects":     getattr(c, "projects", "") or "",
        "project":      getattr(c, "projects", "") or "",
        "target role":  getattr(c, "target_role", "") or "",
        "desired position": getattr(c, "target_role", "") or "",
        "job title":    getattr(c, "target_role", "") or "",
        # linkedin / portfolio
        "linkedin":     c.linkedin_url or "",
        "linkedin url": c.linkedin_url or "",
        "linkedin profile": c.linkedin_url or "",
        "linkedin profile url": c.linkedin_url or "",
        "social media": c.linkedin_url or "",
        "website":      c.linkedin_url or "",
        "portfolio":    c.linkedin_url or "",
        "portfolio link": c.linkedin_url or "",   # matches E-Logic "portfolio_link"
        "enter link to your portfolio": c.linkedin_url or "",  # matches placeholder
    }


# ── Layer 2 – Semantic selector fill ─────────────────────────────────────────

async def _semantic_fill(page: Page, candidate: CandidateProfile) -> int:
    """
    Find form inputs by aria-label, placeholder, name, or id attribute and
    fill them when the attribute value fuzzy-matches a profile key.
    """
    field_map    = _profile_as_field_map(candidate)
    filled_count = 0

    # Query all text-like inputs and textareas
    inputs = await page.query_selector_all(
        "input:not([type='hidden']):not([type='file']):not([type='submit'])"
        ":not([type='button']):not([type='checkbox']):not([type='radio']), "
        "textarea"
    )

    for inp in inputs:
        try:
            if not await inp.is_visible() or not await inp.is_enabled():
                continue

            # Gather all candidate hint strings from the element's attributes
            hints: List[str] = []
            for attr in ("aria-label", "placeholder", "name", "id", "autocomplete"):
                v = await inp.get_attribute(attr)
                if v:
                    hints.append(v.lower().replace("-", " ").replace("_", " "))

            # Also check the label element that references this input
            input_id = await inp.get_attribute("id")
            if input_id:
                lbl = await page.query_selector(f"label[for='{input_id}']")
                if lbl:
                    lbl_text = await lbl.inner_text()
                    hints.append(lbl_text.lower().strip())

            # Find the best matching profile key
            value = _best_match(hints, field_map)
            if value is not None:
                current = await inp.input_value()
                if not current:         # don't overwrite pre-filled values
                    await inp.fill(str(value))
                    # Dispatch React/Ant Design events so form state updates
                    try:
                        await inp.evaluate("""el => {
                            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            )?.set || Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value'
                            )?.set;
                            if (nativeInputValueSetter) {
                                nativeInputValueSetter.call(el, el.value);
                            }
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
                        }""")
                    except Exception:
                        pass
                    await human_delay(0.4, 0.9)
                    filled_count += 1
        except Exception as exc:
            logger.debug(f"Semantic fill error on input: {exc}")

    return filled_count


def _best_match(hints: List[str], field_map: Dict[str, str]) -> Optional[str]:
    """
    Return the profile value whose key is best contained in any hint string.
    Returns None if no good match is found or if the matched value is empty.
    """
    # Longer keys are more specific – match them first
    for key in sorted(field_map.keys(), key=len, reverse=True):
        for hint in hints:
            if key in hint or hint in key:
                val = field_map[key]
                return val if val else None
    return None


# ── Layer 4 – Dropdown / select handling ──────────────────────────────────────

async def _fill_dropdowns(page, candidate: CandidateProfile) -> int:
    """Try to select appropriate values in dropdown/select elements."""
    filled = 0
    try:
        selects = await page.query_selector_all("select")
        for sel_el in selects:
            try:
                if not await sel_el.is_visible() or not await sel_el.is_enabled():
                    continue

                # Get hints from the select element
                hints = []
                for attr in ("aria-label", "name", "id"):
                    v = await sel_el.get_attribute(attr)
                    if v:
                        hints.append(v.lower().replace("-", " ").replace("_", " "))

                # Check label
                sel_id = await sel_el.get_attribute("id")
                if sel_id:
                    lbl = await page.query_selector(f"label[for='{sel_id}']")
                    if lbl:
                        hints.append((await lbl.inner_text()).lower().strip())

                hint_str = " ".join(hints)

                # Get available options
                options = await sel_el.query_selector_all("option")
                option_values = []
                for opt in options:
                    val = await opt.get_attribute("value")
                    text = (await opt.inner_text()).strip()
                    option_values.append((val or "", text.lower()))

                # Try to match based on field type
                selected = False
                if any(k in hint_str for k in ["country", "nation"]):
                    # Try to find India or US in options
                    location = (candidate.location or "").lower()
                    for val, text in option_values:
                        if "india" in text or "india" in location:
                            if "india" in text:
                                await sel_el.select_option(value=val)
                                filled += 1
                                selected = True
                                break
                        elif "united states" in text or "usa" in text or "us" in text:
                            if "us" in location or "usa" in location:
                                await sel_el.select_option(value=val)
                                filled += 1
                                selected = True
                                break

                elif any(k in hint_str for k in ["experience", "years"]):
                    # Select a mid-range experience option
                    for val, text in option_values:
                        if any(yr in text for yr in ["0", "1", "2", "entry", "junior", "fresher"]):
                            await sel_el.select_option(value=val)
                            filled += 1
                            selected = True
                            break

                elif any(k in hint_str for k in ["source", "how did you hear", "referral"]):
                    for val, text in option_values:
                        if "linkedin" in text:
                            await sel_el.select_option(value=val)
                            filled += 1
                            selected = True
                            break

                if selected:
                    await human_delay(0.3, 0.7)
            except Exception as exc:
                logger.debug(f"Dropdown fill error: {exc}")
    except Exception as exc:
        logger.debug(f"Dropdown query error: {exc}")
    return filled


# ── Layer 2b – React phone input ──────────────────────────────────────────────

async def _fill_react_phone_input(page: Page, candidate: CandidateProfile) -> int:
    """Handle react-tel-input and similar custom phone components.
    These render a visible <input type='tel'> but hide the real form input.
    We need to type into the visible input and dispatch events."""
    filled = 0
    if not candidate.phone:
        return 0

    try:
        # Find react-tel-input containers
        phone_inputs = await page.query_selector_all(
            ".react-tel-input input[type='tel'], "
            "input.form-control[type='tel'], "
            "input[type='tel']"
        )
        for inp in phone_inputs:
            try:
                if not await inp.is_visible():
                    continue
                current = await inp.input_value()
                # If only country code is present (e.g. "+91"), append the phone number
                phone_num = candidate.phone.strip()
                if current and current.startswith("+") and len(current) <= 5:
                    # Has country code, just type the number part
                    # Remove the country code from candidate phone if it starts with +
                    if phone_num.startswith(current):
                        phone_num = phone_num[len(current):]
                    elif phone_num.startswith("+"):
                        # Different country code, clear and type full number
                        await inp.click(click_count=3)
                        await inp.type(phone_num, delay=50)
                        filled += 1
                        continue
                    await inp.click()
                    await inp.press("End")
                    await inp.type(phone_num, delay=50)
                elif not current:
                    await inp.type(phone_num, delay=50)
                else:
                    continue  # Already filled
                # Dispatch events for React
                await inp.evaluate("""el => {
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }""")
                await human_delay(0.3, 0.7)
                filled += 1
                logger.info(f"Filled react phone input with: {phone_num}")
                break  # Only fill the first phone input
            except Exception as exc:
                logger.debug(f"React phone input fill error: {exc}")
    except Exception as exc:
        logger.debug(f"React phone query error: {exc}")
    return filled


# ── Layer 3 – Ant Design custom select handling ──────────────────────────────

async def _fill_ant_design_selects(page: Page, candidate: CandidateProfile) -> int:
    """Handle Ant Design <Select> components which render as div.ant-select
    instead of native <select> elements. These need click-to-open + option click."""
    filled = 0
    field_map = _profile_as_field_map(candidate)

    try:
        # Find all Ant Design select components
        ant_selects = await page.query_selector_all(
            "div.ant-select:not(.ant-select-disabled)"
        )
        for sel_el in ant_selects:
            try:
                if not await sel_el.is_visible():
                    continue

                # Get hints from id, label, or surrounding text
                hints: List[str] = []
                sel_id = await sel_el.get_attribute("id")
                if sel_id:
                    hints.append(sel_id.lower().replace("-", " ").replace("_", " ").replace(".", " "))
                    # Check for associated label
                    lbl = await page.query_selector(f"label[for='{sel_id}']")
                    if lbl:
                        lbl_text = await lbl.inner_text()
                        hints.append(lbl_text.lower().strip())

                # Also check parent container for label text
                try:
                    parent = await sel_el.evaluate_handle("el => el.closest('.ant-row.ant-form-item')")
                    if parent:
                        parent_label = await parent.query_selector("label")
                        if parent_label:
                            hints.append((await parent_label.inner_text()).lower().strip())
                except Exception:
                    pass

                hint_str = " ".join(hints)
                if not hint_str:
                    continue

                # Determine what value to select
                desired_value = None
                if any(k in hint_str for k in ["country", "nation"]):
                    location = (candidate.location or "").lower()
                    if "india" in location:
                        desired_value = "India"
                    elif "us" in location or "usa" in location or "united states" in location:
                        desired_value = "United States"
                    else:
                        # Try to extract country from location string
                        parts = (candidate.location or "").split(",")
                        if parts:
                            desired_value = parts[-1].strip()
                elif any(k in hint_str for k in ["certificate", "degree", "education level"]):
                    desired_value = "Bachelor"
                elif any(k in hint_str for k in ["experience", "years"]):
                    desired_value = candidate.years_of_experience or "2"
                elif any(k in hint_str for k in ["source", "how did you hear", "referral"]):
                    desired_value = "LinkedIn"
                else:
                    # Try generic matching
                    value = _best_match(hints, field_map)
                    if value:
                        desired_value = value

                if not desired_value:
                    continue

                # Click the select to open dropdown
                selection_div = await sel_el.query_selector(".ant-select-selection")
                if selection_div:
                    await selection_div.click()
                else:
                    await sel_el.click()
                await human_delay(0.5, 1.0)

                # Wait for dropdown popup to appear
                await page.wait_for_selector(
                    "div.ant-select-dropdown:not(.ant-select-dropdown-hidden)",
                    timeout=3000
                )

                # Find and click the matching option
                options = await page.query_selector_all(
                    "div.ant-select-dropdown:not(.ant-select-dropdown-hidden) li.ant-select-dropdown-menu-item, "
                    "div.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item"
                )
                clicked = False
                for opt in options:
                    try:
                        opt_text = (await opt.inner_text()).strip()
                        if desired_value.lower() in opt_text.lower() or opt_text.lower() in desired_value.lower():
                            await opt.click()
                            await human_delay(0.3, 0.7)
                            filled += 1
                            clicked = True
                            logger.info(f"Ant Design select '{hint_str}': selected '{opt_text}'")
                            break
                    except Exception:
                        continue

                if not clicked:
                    # Close the dropdown by pressing Escape
                    await page.keyboard.press("Escape")
                    logger.debug(f"No matching option for '{desired_value}' in Ant select '{hint_str}'")

            except Exception as exc:
                logger.debug(f"Ant Design select fill error: {exc}")
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
    except Exception as exc:
        logger.debug(f"Ant Design select query error: {exc}")
    return filled


# ── Layer 5 – Basic resume upload (consistent signature) ─────────────────────

async def _upload_resume_basic(page: Page, resume_path: str) -> int:
    """Find any file-input element and set the resume file path.
    Returns 1 if an upload was performed, else 0.
    This is the basic version used in the semantic fill pipeline."""
    try:
        await page.wait_for_selector("input[type='file']", state="attached", timeout=5000)
    except Exception:
        pass
    
    selectors = [
        "input[type='file'][id*='resume' i]",
        "input[type='file'][name*='resume' i]",
        "input[type='file'][aria-label*='resume' i]",
        "input[type='file'][id*='cv' i]",
        "input[type='file'][name*='cv' i]",
        "input[type='file'][aria-label*='cv' i]",
        "[aria-label*='resume' i] input[type='file']",
        "[aria-label*='cv' i] input[type='file']",
        "input[type='file'][accept*='pdf']",
        "input[type='file'][accept*='.doc']",
        "input[type='file']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.set_input_files(resume_path)
                await human_delay(1.0, 2.0)
                logger.info(f"Resume uploaded via selector: {sel!r}")
                return 1
        except Exception as exc:
            logger.debug(f"File upload attempt failed ({sel!r}): {exc}")
    return 0


# ── Layer 6 – Dispatch React/Ant Design events ──────────────────────────────

async def _dispatch_react_events(page: Page) -> None:
    """After filling inputs with Playwright's fill(), dispatch native DOM events
    that React/Ant Design form decorators listen to. Without these, the form's
    internal state may still show empty values even though the DOM shows text."""
    try:
        await page.evaluate("""() => {
            const inputs = document.querySelectorAll(
                'input:not([type="hidden"]):not([type="file"]):not([type="submit"]):not([type="button"]), textarea'
            );
            inputs.forEach(input => {
                if (input.value && input.value.trim()) {
                    // Trigger React synthetic event system
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set || Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set;
                    if (nativeInputValueSetter) {
                        nativeInputValueSetter.call(input, input.value);
                    }
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                }
            });
        }""")
        logger.info("Dispatched React/Ant Design form events on all filled inputs")
    except Exception as exc:
        logger.debug(f"React event dispatch failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  Known-portal adapters (Layer 1)
# ═══════════════════════════════════════════════════════════════════════════════


@_register(r"myworkday|workday\.com")
async def _workday(page: Page, candidate: CandidateProfile) -> int:
    """Workday – used by ~40 % of Fortune 500 companies."""
    filled = 0
    try:
        # Workday renders inside an iframe on some domains
        frames = [page] + list(page.frames)
        for frame in frames:
            try:
                # Step 1: "Apply" landing button if present
                apply_btn = await frame.query_selector(
                    "button[data-automation-id='applyButton'], "
                    "a[data-automation-id='applyButton']"
                )
                if apply_btn:
                    await apply_btn.click()
                    await human_delay(2.0, 3.5)

                # Step 2: Fill known Workday field automation-ids
                wd_fields = {
                    "input[data-automation-id='legalName--firstName']":  _first(candidate.name),
                    "input[data-automation-id='legalName--lastName']":   _last(candidate.name),
                    "input[data-automation-id='email']":                 candidate.email,
                    "input[data-automation-id='phone']":                 candidate.phone,
                    "input[data-automation-id='addressSection--city']":  _city(candidate.location),
                }
                for sel, val in wd_fields.items():
                    if not val:
                        continue
                    el = await frame.query_selector(sel)
                    if el and await el.is_visible():
                        await el.fill(val)
                        await human_delay(0.3, 0.7)
                        filled += 1

                # Cover letter / summary textarea
                for ta_sel in [
                    "textarea[data-automation-id='coverLetter']",
                    "textarea[data-automation-id='message']",
                ]:
                    ta = await frame.query_selector(ta_sel)
                    if ta and candidate.cover_text:
                        await ta.fill(candidate.cover_text)
                        filled += 1
                        break
            except Exception as exc:
                logger.debug(f"Workday frame fill error: {exc}")
    except Exception as exc:
        logger.warning(f"Workday adapter error: {exc}")
    return filled


@_register(r"greenhouse\.io|boards\.greenhouse")
async def _greenhouse(page: Page, candidate: CandidateProfile) -> int:
    """Greenhouse – common in tech companies."""
    filled = 0
    try:
        gh_fields = {
            "#first_name":      _first(candidate.name),
            "#last_name":       _last(candidate.name),
            "#email":           candidate.email,
            "#phone":           candidate.phone,
            "#location":        candidate.location or "",
            "#cover_letter_text": candidate.cover_text,
        }
        for sel, val in gh_fields.items():
            if not val:
                continue
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(val)
                await human_delay(0.3, 0.7)
                filled += 1

        # LinkedIn URL field
        if candidate.linkedin_url:
            for sel in ["#job_application_answers_question_linkedin_profile",
                        "input[name*='linkedin']"]:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(candidate.linkedin_url)
                    filled += 1
                    break
    except Exception as exc:
        logger.warning(f"Greenhouse adapter error: {exc}")
    return filled


@_register(r"lever\.co|jobs\.lever")
async def _lever(page: Page, candidate: CandidateProfile) -> int:
    """Lever – widely used by mid-size tech companies."""
    filled = 0
    try:
        lever_fields = {
            "input[name='name']":        candidate.name,
            "input[name='email']":       candidate.email,
            "input[name='phone']":       candidate.phone,
            "input[name='location']":    candidate.location or "",
            "input[name='urls[LinkedIn]']": candidate.linkedin_url or "",
            "textarea[name='comments']": candidate.cover_text,
        }
        for sel, val in lever_fields.items():
            if not val:
                continue
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(val)
                await human_delay(0.3, 0.7)
                filled += 1
    except Exception as exc:
        logger.warning(f"Lever adapter error: {exc}")
    return filled


@_register(r"taleo\.net|oracle\.taleo|icims\.com")
async def _taleo_icims(page: Page, candidate: CandidateProfile) -> int:
    """Taleo (Oracle) / iCIMS – enterprise ATS platforms."""
    filled = 0
    try:
        # Both portals use similar label-based inputs
        label_map = {
            "First Name":   _first(candidate.name),
            "Last Name":    _last(candidate.name),
            "Email":        candidate.email,
            "Phone":        candidate.phone,
            "City":         _city(candidate.location),
            "Cover Letter": candidate.cover_text,
        }
        for label_text, value in label_map.items():
            if not value:
                continue
            # Find label element, then get the associated input
            lbl = await page.query_selector(f"label:has-text('{label_text}')")
            if not lbl:
                continue
            for_attr = await lbl.get_attribute("for")
            if for_attr:
                inp = await page.query_selector(f"#{for_attr}, [name='{for_attr}']")
                if inp and await inp.is_visible():
                    await inp.fill(value)
                    await human_delay(0.3, 0.7)
                    filled += 1
    except Exception as exc:
        logger.warning(f"Taleo/iCIMS adapter error: {exc}")
    return filled


@_register(r"successfactors|sap\.com/careers")
async def _sap(page: Page, candidate: CandidateProfile) -> int:
    """SAP SuccessFactors – large enterprise orgs."""
    filled = 0
    try:
        sap_fields = {
            "input[id*='firstName']": _first(candidate.name),
            "input[id*='lastName']":  _last(candidate.name),
            "input[id*='email']":     candidate.email,
            "input[id*='phone']":     candidate.phone,
        }
        for sel, val in sap_fields.items():
            if not val:
                continue
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(val)
                await human_delay(0.3, 0.7)
                filled += 1
    except Exception as exc:
        logger.warning(f"SAP adapter error: {exc}")
    return filled


@_register(r"capgemini\.com|careers\.capgemini")
async def _capgemini(page: Page, candidate: CandidateProfile) -> int:
    """Capgemini careers – try to click the Apply flow, fill frames, upload resume."""
    filled = 0
    try:
        # Try to activate the 'Apply' flow if present
        for sel in ["button:has-text('Apply')", "a:has-text('Apply')", "button.apply", "a.apply"]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await human_delay(1.5, 3.0)
                    break
            except Exception:
                continue

        # Attempt semantic fill across main page and any frames
        frames = [page] + list(page.frames)
        for frame in frames:
            try:
                filled += await _semantic_fill(frame, candidate)
            except Exception:
                pass

        # Try file upload in frames
        if candidate.resume_path:
            for frame in frames:
                try:
                    uploaded = await _upload_resume(frame, candidate.resume_path)
                    if uploaded:
                        filled += uploaded
                        break
                except Exception:
                    pass

    except Exception as exc:
        logger.warning(f"Capgemini adapter error: {exc}")
    return filled


# ── Small utility helpers ─────────────────────────────────────────────────────

def _first(name: str) -> str:
    parts = name.strip().split(" ", 1)
    return parts[0]

def _last(name: str) -> str:
    parts = name.strip().split(" ", 1)
    return parts[1] if len(parts) > 1 else ""

def _city(location: Optional[str]) -> str:
    if not location:
        return ""
    return location.split(",")[0].strip()


# ── Universal AI Handler (Fallback) ──────────────────────────────────────────

async def _ai_fallback_handler(page: Page, candidate: CandidateProfile, login_credentials: Optional[Any], emit: Optional[Any] = None, session_id: str = "default") -> int:
    from .config import get_current_gemini_key, get_portal_credentials, GEMINI_MODEL
    from .browser import human_delay
    from pathlib import Path
    import json
    
    current_key = get_current_gemini_key()
    if not current_key:
        logger.warning("GEMINI_API_KEY not set. Cannot run Universal AI Handler.")
        return 0
        
    import google.generativeai as genai
    genai.configure(api_key=current_key)
    client = genai.GenerativeModel(GEMINI_MODEL)

    filled_count = 0
    
    for step in range(8):
        if emit:
            emit({"type": "log", "level": "info", "message": f"AI Handler Step {step + 1}/8..."})
        
        await human_delay(2.0, 4.0)
        
        # 1. Classify page
        page_type, reasoning = await classify_portal_page(page, client)
        logger.info(f"AI classified page as {page_type}: {reasoning}")
        if emit:
            emit({"type": "log", "level": "info", "message": f"Page classified as: {page_type}"})

        # Check for bypass buttons first
        bypass_selectors = [
            "button:has-text('Apply without an Account')",
            "a:has-text('Apply without an Account')",
            "button:has-text('Apply Manually')",
            "a:has-text('Apply Manually')",
            "button:has-text('Continue without account')",
            "a:has-text('Continue without account')"
        ]
        bypassed = False
        for b_sel in bypass_selectors:
            try:
                btn = await page.query_selector(b_sel)
                if btn and await btn.is_visible():
                    logger.info(f"Clicking bypass button: {b_sel}")
                    if emit:
                        emit({"type": "log", "level": "info", "message": f"Found bypass button, clicking to skip login..."})
                    await btn.click()
                    await human_delay(2.0, 4.0)
                    bypassed = True
                    break
            except Exception:
                pass
                
        if bypassed:
            continue

        if page_type == "CAPTCHA":
            if emit:
                emit({"type": "manual_login_required", "message": "CAPTCHA detected. Please solve it manually."})
            break
        elif page_type == "CONFIRMATION":
            logger.info("Confirmation page detected — application appears submitted!")
            if emit:
                emit({"type": "log", "level": "info", "message": "✓ Confirmation page detected — application submitted!"})
            # Take a screenshot of the confirmation page
            try:
                import os, time as _time
                os.makedirs("screenshots", exist_ok=True)
                ss_path = f"screenshots/confirmation_{session_id}_{int(_time.time())}.png"
                await page.screenshot(path=ss_path, full_page=True)
                if emit:
                    emit({"type": "log", "level": "info", "message": f"Confirmation screenshot saved: {ss_path}"})
            except Exception:
                pass
            break  # Done — application submitted
        elif page_type == "UNKNOWN":
            # Auto-retry: wait for page to finish loading, then re-classify
            logger.info("Page classified as UNKNOWN — retrying after wait...")
            if emit:
                emit({"type": "log", "level": "info", "message": "Page type unclear — waiting for page to load fully..."})
            await human_delay(3.0, 5.0)
            
            # Re-classify after waiting
            page_type, reasoning = await classify_portal_page(page, client)
            logger.info(f"Re-classification after wait: {page_type}")
            
            if page_type != "UNKNOWN":
                # Page resolved — re-process in the next loop iteration
                if emit:
                    emit({"type": "log", "level": "info", "message": f"Page resolved to: {page_type} — continuing..."})
                continue
            
            # Still UNKNOWN — check if there are any fillable form inputs
            has_inputs = False
            try:
                inputs = await page.query_selector_all("input:not([type='hidden']):not([type='submit']), select, textarea")
                visible_count = 0
                for inp in inputs[:20]:
                    try:
                        if await inp.is_visible():
                            visible_count += 1
                    except Exception:
                        pass
                has_inputs = visible_count >= 1
            except Exception:
                pass
            
            if has_inputs:
                # There are form inputs — treat as APPLICATION_FORM and try to fill
                logger.info(f"UNKNOWN page has {visible_count} visible inputs — treating as APPLICATION_FORM")
                if emit:
                    emit({"type": "log", "level": "info", "message": f"Found {visible_count} form inputs on unknown page — attempting to fill..."})
                page_type = "APPLICATION_FORM"
                continue  # Re-process as APPLICATION_FORM
            else:
                # Truly no inputs — only NOW pause for manual intervention
                logger.info("Truly unknown page with no inputs — pausing for manual intervention.")
                await pause_checkpoint(session_id, emit, "UNKNOWN page type with no form inputs — please review and navigate manually, then resume.", page=page)
                page_type, reasoning = await classify_portal_page(page, client)
                if page_type == "UNKNOWN":
                    break
        elif page_type == "JOB_DESCRIPTION":
            logger.info("Page is a job description. Looking for an Apply button...")
            clicked = False
            for sel in ["a:has-text('Apply')", "button:has-text('Apply')", "a[href*='apply']", "button[title*='Apply']", "[aria-label*='Apply']", ".apply-button"]:
                try:
                    btns = await page.query_selector_all(sel)
                    for btn in btns:
                        if await btn.is_visible():
                            await btn.click()
                            try:
                                await page.wait_for_load_state("networkidle", timeout=5000)
                            except:
                                pass
                            clicked = True
                            break
                    if clicked: break
                except Exception:
                    pass
            if not clicked:
                await pause_checkpoint(session_id, emit, "Could not find Apply button on job description. Please click it manually.", page=page)
            continue
        elif page_type in ("LOGIN_PAGE", "REGISTER_PAGE", "LOGIN_OR_REGISTER"):
            is_register = page_type in ("REGISTER_PAGE",)
            creds = get_portal_credentials(page.url, login_credentials, for_registration=is_register)
            email = creds.get("email")
            password = creds.get("password")
            
            if not email or not password:
                logger.warning("Missing portal credentials for login/register.")
                break
            
            logger.info(f"Using {'generated' if is_register else 'actual'} password for {page_type}")
            if emit:
                emit({"type": "log", "level": "info", "message": f"Attempting {page_type.lower().replace('_', ' ')} with portal credentials..."})
                
            fields = await map_form_fields(page, candidate, client, page_type, email, password, emit, session_id)
            if not fields:
                logger.warning("No fields mapped for login/register.")
                break
                
            filled_count += await fill_and_submit_mapped_fields(page, fields, candidate, client, emit, session_id)
            
        elif page_type == "APPLICATION_FORM":
            # Upgrade 9: Form Before Screenshot
            try:
                from .diagnostics import DIAGNOSTICS_DIR
                await page.screenshot(path=str(DIAGNOSTICS_DIR / "form_before.png"), full_page=True)
                if emit: emit({"type": "log", "level": "info", "message": "Screenshot saved: form_before.png"})
            except Exception as e:
                logger.debug(f"Failed to capture form_before.png: {e}")

            fields = await map_form_fields(page, candidate, client, page_type, "", "", emit, session_id)
            if fields:
                gemini_count = sum(1 for f in fields if f.get('source') == 'GEMINI')
                filled_count += await fill_and_submit_mapped_fields(page, fields, candidate, client, emit, session_id)
                if gemini_count > 3:
                    logger.info(f"AI guessed {gemini_count} fields — continuing without pause (expected behavior)")
                    if emit:
                        emit({"type": "log", "level": "info", "message": f"AI filled {gemini_count} fields automatically"})
            else:
                logger.info("No application fields found to fill. Attempting to upload resume.")
                if candidate.resume_path:
                    filled_count += await _upload_resume(page, candidate.resume_path, emit)
                    
            # Upgrade 6: Pre-submit validation
            await _pre_submit_validation(page, candidate, client, emit, session_id)

            # Upgrade 9: Form After Screenshot
            try:
                from .diagnostics import DIAGNOSTICS_DIR
                await page.screenshot(path=str(DIAGNOSTICS_DIR / "form_after.png"), full_page=True)
                if emit: emit({"type": "log", "level": "info", "message": "Screenshot saved: form_after.png"})
            except Exception as e:
                logger.debug(f"Failed to capture form_after.png: {e}")
                
            # Upgrade 5: reCAPTCHA detection
            await _handle_recaptcha_before_submit(page, emit, session_id)

            # Try to click next/continue/submit
            # Auto-submit without pausing — only pause on actual errors
            if emit:
                emit({"type": "log", "level": "info", "message": "Clicking Next/Submit..."})
            url_before = page.url
            clicked = await click_next_or_submit(page)
            if not clicked:
                logger.info("No 'Next' or 'Submit' button found. Form may be complete.")
                break

            await human_delay(1.5, 2.5)

            # Validation-aware retry: if the page didn't advance and shows field
            # errors, re-map only what's wrong, fill it, and submit once more.
            errors = await _collect_validation_errors(page)
            if errors and page.url == url_before:
                if emit:
                    emit({"type": "log", "level": "warning",
                          "message": f"Form rejected ({len(errors)} field error(s)) — fixing and retrying..."})
                logger.info(f"Validation errors after submit: {errors[:5]}")
                refields = await map_form_fields(page, candidate, client, "APPLICATION_FORM", "", "", emit, session_id)
                if refields:
                    filled_count += await fill_and_submit_mapped_fields(page, refields, candidate, client, emit, session_id)
                await _handle_recaptcha_before_submit(page, emit, session_id)
                await click_next_or_submit(page)
                await human_delay(1.5, 2.5)

            # Upgrade 7: Post-submit confirmation (returns status; never raises)
            status = await _post_submit_confirmation(page, emit)
            if status == "CONFIRMED":
                logger.info("Application confirmed — finishing AI handler.")
                break

    return filled_count

async def ai_answer_unknown_field(field_label: str, field_type: str, candidate: CandidateProfile, api_key: str) -> str:
    """Upgrade 2: AI-powered field answering for unknown questions using Gemini."""
    import google.generativeai as genai
    import json
    from .config import get_current_gemini_key, GEMINI_MODEL
    genai.configure(api_key=get_current_gemini_key())
    model = genai.GenerativeModel(GEMINI_MODEL)
    
    profile_json = candidate.model_dump_json(exclude={'resume_path'})
    prompt_string = f"""You are filling a job application form on behalf of a candidate. The field label is: {field_label}. The field type is: {field_type}. Here is everything known about the candidate as JSON: {profile_json}. Based on this, what is the most appropriate answer for this field? Rules: if the candidate profile does not contain a direct answer, use the safest most common answer that would not disqualify them. For yes/no questions about things not in the profile such as security clearance, default to No. For location preference, use the candidate's stored preferred_location or location field. For citizenship and work authorization questions, use the work_authorization field. For questions about salary, use salary_expectation field or leave blank if empty. Return ONLY the answer value as a plain string with absolutely no explanation, no punctuation around it, no quotes."""
    
    try:
        response = await _call_gemini_with_retry(model, prompt_string)
        return response.text.strip()
    except Exception as e:
        logger.error(f"ai_answer_unknown_field failed for {field_label}: {e}")
        return ""

async def fill_dropdown(page: Page, selector: str, desired_value: str, known_options: Optional[List[str]] = None) -> bool:
    """Select *desired_value* in a dropdown (native <select>, ARIA listbox/combobox,
    or react-select). Uses fuzzy/synonym matching against the option labels.
    Returns True if a selection was made."""
    from .browser import human_delay
    try:
        el = await page.query_selector(selector)
        if not el:
            return False
        tag = await el.evaluate("e => e.tagName.toLowerCase()")
        role = (await el.evaluate("e => e.getAttribute('role') || ''")).lower()

        # Type 1: Native HTML <select>
        if tag == "select":
            options = await el.evaluate("e => Array.from(e.options).map(o => ({text: o.innerText, value: o.value}))")
            texts = [o["text"] for o in options if o["text"]]
            match = _best_option_match(desired_value, texts) or (known_options and _best_option_match(desired_value, known_options))
            if match:
                try:
                    await el.select_option(label=match)
                    return True
                except Exception:
                    # match against value attribute as fallback
                    for o in options:
                        if o["text"] == match:
                            await el.select_option(value=o["value"])
                            return True
            return False

        # Type 2: Custom div dropdown (role=listbox/combobox, not an input)
        if tag != "input" and ("listbox" in role or "combobox" in role):
            await el.click()
            await human_delay(0.4, 0.7)
            opts = await page.query_selector_all("li[role='option'], [role='option'], li, .option, .item, [class*='option']")
            visible = []
            for o in opts:
                try:
                    if await o.is_visible():
                        visible.append((await o.inner_text(), o))
                except Exception:
                    pass
            match = _best_option_match(desired_value, [t for t, _ in visible if t])
            for t, o in visible:
                if t == match:
                    await o.click()
                    return True
            return False

        # Type 3: Combobox / react-select text input — type then pick a suggestion
        if tag == "input":
            await el.click()
            try:
                await el.fill("")
            except Exception:
                pass
            await el.type(desired_value, delay=40)
            await human_delay(0.7, 1.0)
            opts = await page.query_selector_all("li[role='option'], [role='option'], li, [class*='option']")
            visible = []
            for o in opts:
                try:
                    if await o.is_visible():
                        visible.append((await o.inner_text(), o))
                except Exception:
                    pass
            if visible:
                match = _best_option_match(desired_value, [t for t, _ in visible if t])
                for t, o in visible:
                    if t == match:
                        await o.click()
                        return True
                # No fuzzy match — accept the first suggestion
                await visible[0][1].click()
                return True
            # No suggestions appeared — press Enter to accept typed value
            try:
                await el.press("Enter")
                return True
            except Exception:
                return False
        return False
    except Exception as e:
        logger.error(f"fill_dropdown failed for {selector}: {e}")
        return False

async def _upload_resume(page: Page, resume_path: str, emit: Optional[Any] = None) -> int:
    """Upgrade 4: Dual resume upload handling."""
    from .browser import human_delay
    try:
        # Check for 'Attach resume' button
        attach_btns = await page.query_selector_all("button, a")
        clicked_attach = False
        for btn in attach_btns:
            text = await btn.inner_text()
            if text and ("attach resume" in text.lower() or "attach" in text.lower()):
                await btn.click()
                await human_delay(1.0, 1.0)
                clicked_attach = True
                break
        
        # Standard fallback inputs
        try:
            await page.wait_for_selector("input[type='file']", state="attached", timeout=5000)
        except Exception:
            pass
            
        selectors = [
            "input[type='file']",
            "input[accept*='pdf']",
            "input[accept*='resume']"
        ]
        
        uploaded = False
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.set_input_files(resume_path)
                    await human_delay(2.0, 2.0)
                    logger.info(f"Resume uploaded via selector: {sel!r}")
                    uploaded = True
                    break
            except Exception:
                pass
                
        if uploaded:
            # Verify filename appears on page
            import os
            filename = os.path.basename(resume_path)
            body_text = await page.inner_text("body")
            if filename not in body_text:
                if emit: emit({"type": "log", "level": "warning", "message": "Resume upload may have failed — filename not found on page after upload"})
            return 1
            
    except Exception as exc:
        logger.debug(f"Dual upload logic failed: {exc}")
    return 0

async def _collect_validation_errors(page: Page) -> List[str]:
    """Return visible validation-error messages on the page (empty if none)."""
    errors: List[str] = []
    try:
        for frame in page.frames:
            try:
                msgs = await frame.evaluate(
                    """() => {
                        const sels = ["[aria-invalid='true']", ".error", ".error-message",
                            ".field-error", ".invalid-feedback", "[class*='error']",
                            "[role='alert']", ".Mui-error", ".has-error"];
                        const out = [];
                        for (const s of sels) {
                            for (const el of document.querySelectorAll(s)) {
                                if (el.offsetParent === null) continue;
                                const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
                                if (t && t.length < 200) out.push(t);
                            }
                        }
                        return out;
                    }"""
                )
                if msgs:
                    errors.extend(msgs)
            except Exception:
                pass
    except Exception:
        pass
    # De-duplicate while preserving order
    seen, uniq = set(), []
    for e in errors:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


async def _pre_submit_validation(page: Page, candidate: CandidateProfile, client: Any, emit: Optional[Any], session_id: str = "default") -> int:
    """Pre-submit sweep: re-map the form with the robust engine to fill any
    required/empty fields (including radios, checkboxes and custom dropdowns)
    that the first pass missed or that appeared after earlier fills."""
    try:
        fields = await map_form_fields(page, candidate, client, "APPLICATION_FORM", "", "", emit, session_id)
        if not fields:
            return 0
        n = await fill_and_submit_mapped_fields(page, fields, candidate, client, emit, session_id)
        if n and emit:
            emit({"type": "log", "level": "info", "message": f"Pre-submit pass filled {n} remaining field(s)."})
        return n
    except Exception as e:
        logger.error(f"Pre-submit validation failed: {e}")
        return 0

async def _handle_recaptcha_before_submit(page: Page, emit: Optional[Any], session_id: str = "default"):
    """Upgrade 5: reCAPTCHA detection and handling."""
    try:
        recaptcha_iframe = await page.query_selector('iframe[src*="recaptcha"], div.g-recaptcha, div[data-sitekey]')
        is_robot_text = await page.evaluate("() => document.body.innerText.includes('I am not a robot')")

        if recaptcha_iframe or is_robot_text:
            if recaptcha_iframe:
                box = await recaptcha_iframe.bounding_box()
                if box:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    await asyncio.sleep(3)

            # Check success
            checked = await page.query_selector("div.recaptcha-checkbox-checked")
            if checked:
                if emit: emit({"type": "log", "level": "info", "message": "reCAPTCHA checkbox clicked successfully"})
                return

            # Check for visual challenge
            challenge = await page.query_selector('iframe[title*="challenge"], iframe[src*="bframe"]')
            if challenge:
                await pause_checkpoint(session_id, emit, "reCAPTCHA image challenge detected — please solve manually in the browser window, then resume.", page=page)
    except Exception as e:
        logger.error(f"reCAPTCHA handling error: {e}")

async def _post_submit_confirmation(page: Page, emit: Optional[Any]) -> str:
    """Detect whether the application was submitted. Returns 'CONFIRMED' or
    'SUBMITTED_UNCONFIRMED'. Never raises — multi-step forms must keep going."""
    try:
        success_indicators = ["confirmation", "thank-you", "thank_you", "success", "submitted", "complete", "apply-complete"]
        success_phrases = ["application submitted", "thank you for applying", "we have received your application", "application complete", "you have successfully applied", "your application has been received"]

        found_success = False
        for _ in range(5):
            await asyncio.sleep(2)
            url = page.url.lower()
            if any(ind in url for ind in success_indicators):
                found_success = True
                break

            try:
                body_lower = (await page.inner_text('body')).lower()
            except Exception:
                body_lower = ""
            if any(phrase in body_lower for phrase in success_phrases):
                found_success = True
                break

        if found_success:
            if emit: emit({"type": "log", "level": "info", "message": "Application submitted successfully — confirmation detected"})
            return "CONFIRMED"

        # No explicit confirmation — capture a screenshot for review and let the
        # caller decide whether to continue (multi-step) or finish.
        try:
            from .diagnostics import DIAGNOSTICS_DIR
            await page.screenshot(path=str(DIAGNOSTICS_DIR / "submit_result.png"), full_page=True)
        except Exception:
            pass
        if emit:
            emit({"type": "log", "level": "info",
                  "message": "Submit clicked — no confirmation text yet (continuing / flagged for review)."})
        return "SUBMITTED_UNCONFIRMED"
    except Exception as e:
        logger.error(f"Post-submit check error: {e}")
        return "SUBMITTED_UNCONFIRMED"


async def _heuristic_classify_page(page: Page) -> Tuple[str, str]:
    """Classify the page using DOM inspection only — no AI required.
    Returns (page_type, reasoning). Falls back to UNKNOWN if unsure."""
    try:
        url = (page.url or "").lower()

        # ── CONFIRMATION detection ────────────────────────────────────────
        confirmation_url_markers = [
            "confirmation", "thank-you", "thank_you", "thankyou",
            "success", "submitted", "complete", "apply-complete",
            "application-received"
        ]
        if any(m in url for m in confirmation_url_markers):
            return "CONFIRMATION", "URL contains confirmation marker"

        body_text = ""
        try:
            body_text = (await page.inner_text("body")).lower()
        except Exception:
            try:
                body_text = (await page.evaluate("document.body.innerText")).lower()
            except Exception:
                pass

        confirmation_phrases = [
            "thank you for applying", "application submitted",
            "we have received your application", "application complete",
            "you have successfully applied", "your application has been received",
            "thanks for your interest", "thank you for your application",
            "application has been submitted", "successfully submitted",
            "your resume has been submitted", "we'll be in touch",
            "we will review your application",
        ]
        if any(phrase in body_text for phrase in confirmation_phrases):
            return "CONFIRMATION", f"Page contains confirmation phrase"

        # ── CAPTCHA detection ─────────────────────────────────────────────
        try:
            captcha_els = await page.query_selector_all(
                "iframe[src*='captcha'], iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
                "div.g-recaptcha, div.h-captcha, [data-sitekey], "
                "iframe[src*='challenges.cloudflare.com']"
            )
            for el in captcha_els:
                try:
                    if await el.is_visible():
                        return "CAPTCHA", "Visible CAPTCHA widget detected"
                except Exception:
                    continue
        except Exception:
            pass

        title = ""
        try:
            title = (await page.title()).lower()
        except Exception:
            pass
        if "just a moment" in title or "verifying you are human" in title:
            return "CAPTCHA", "Cloudflare challenge page title detected"

        # ── Count visible form elements ───────────────────────────────────
        form_stats = {"text": 0, "email": 0, "tel": 0, "file": 0, "password": 0,
                      "select": 0, "textarea": 0, "checkbox": 0, "radio": 0}
        try:
            form_stats = await page.evaluate("""() => {
                const stats = {text:0, email:0, tel:0, file:0, password:0,
                               select:0, textarea:0, checkbox:0, radio:0};
                const inputs = document.querySelectorAll('input, select, textarea');
                for (const el of inputs) {
                    if (el.offsetParent === null && el.type !== 'file') continue;
                    const t = (el.type || el.tagName).toLowerCase();
                    if (t === 'hidden' || t === 'submit' || t === 'button') continue;
                    if (t in stats) stats[t]++;
                    else if (t === 'text' || !el.type) stats.text++;
                }
                stats.select = document.querySelectorAll('select').length;
                stats.textarea = document.querySelectorAll('textarea').length;
                return stats;
            }""")
        except Exception:
            pass

        total_inputs = sum(form_stats.get(k, 0) for k in ["text", "email", "tel", "textarea", "select"])
        has_password = form_stats.get("password", 0) > 0
        has_file = form_stats.get("file", 0) > 0

        # ── LOGIN page ────────────────────────────────────────────────────
        if has_password and total_inputs <= 2:
            return "LOGIN_PAGE", f"Password field with {total_inputs} other inputs — login form"

        # Multi-step login detection (e.g. Dice, Workday — show email first, then password)
        login_url_markers = ["login", "signin", "sign-in", "sso", "auth", "account/access"]
        is_login_url = any(m in url for m in login_url_markers)
        has_email_input = form_stats.get("email", 0) > 0
        login_text_markers = ["sign in", "log in", "login", "email address", "enter your email",
                              "username", "continue with email", "welcome back"]
        has_login_text = any(m in body_text for m in login_text_markers)

        if (total_inputs <= 1 or has_email_input) and (is_login_url or has_login_text):
            return "LOGIN_PAGE", f"Multi-step login detected (URL: {is_login_url}, text: {has_login_text}, inputs: {total_inputs})"

        # ── APPLICATION_FORM detection ────────────────────────────────────
        if total_inputs >= 2 or has_file:
            return "APPLICATION_FORM", f"Found {total_inputs} form inputs + {form_stats.get('file',0)} file inputs"

        # ── JOB_DESCRIPTION detection ─────────────────────────────────────
        apply_btn_count = 0
        try:
            for sel in ["button:has-text('Apply')", "a:has-text('Apply')", "a[href*='apply']",
                         "button:has-text('Apply Now')", "a:has-text('Apply Now')"]:
                try:
                    els = await page.query_selector_all(sel)
                    for el in els:
                        if await el.is_visible():
                            apply_btn_count += 1
                except Exception:
                    pass
        except Exception:
            pass

        if apply_btn_count > 0 and total_inputs == 0:
            return "JOB_DESCRIPTION", f"Found {apply_btn_count} Apply buttons with no form inputs"

        return "UNKNOWN", "Heuristic could not determine page type"
    except Exception as e:
        return "UNKNOWN", f"Heuristic error: {e}"


async def classify_portal_page(page: Page, client: Any) -> Tuple[str, str]:
    """Classify portal page: try fast heuristics first, then fall back to Gemini AI."""
    # Try heuristic classification first (fast, no API needed)
    heuristic_type, heuristic_reason = await _heuristic_classify_page(page)
    if heuristic_type != "UNKNOWN":
        logger.info(f"Heuristic classification: {heuristic_type} — {heuristic_reason}")
        return heuristic_type, heuristic_reason

    # Fall back to Gemini AI classification
    import json
    try:
        html = await page.evaluate("document.body.innerText")
        content = html[:10000]
        
        prompt = f"""Analyze the following web page text and classify its primary purpose.
        Respond in JSON with 'page_type' and 'reasoning'.
        Valid page_types: LOGIN_PAGE, REGISTER_PAGE, LOGIN_OR_REGISTER, APPLICATION_FORM, CAPTCHA, JOB_DESCRIPTION, CONFIRMATION, UNKNOWN.
        
        Page Text:
        {content}
        """
        
        response = await _call_gemini_with_retry(client, prompt)
        text = response.text.strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        data = json.loads(text[start:end])
        return data.get("page_type", "UNKNOWN"), data.get("reasoning", "")
    except Exception as e:
        logger.error(f"Classification error: {e}")
        # On API error, re-run heuristics with a wait for late-loading content
        await asyncio.sleep(2)
        heuristic_type2, heuristic_reason2 = await _heuristic_classify_page(page)
        if heuristic_type2 != "UNKNOWN":
            logger.info(f"Post-error heuristic reclassification: {heuristic_type2}")
            return heuristic_type2, heuristic_reason2
        return "UNKNOWN", str(e)


# ── Field-matching helpers (used by the AI form engine) ───────────────────────

def _norm(s: Any) -> str:
    """Lower-case, strip punctuation, collapse whitespace — for fuzzy matching."""
    if not s:
        return ""
    s = str(s).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Common equivalences so AI answers like "United States" match an option "USA".
_OPTION_SYNONYMS: List[List[str]] = [
    ["yes", "y", "true", "agree", "i agree", "i certify", "accept"],
    ["no", "n", "false", "disagree"],
    ["united states", "usa", "us", "u s", "u s a", "united states of america", "america"],
    ["united kingdom", "uk", "u k", "great britain"],
    ["male", "m"],
    ["female", "f"],
    ["prefer not to say", "decline to self identify", "i don t wish to answer",
     "i do not wish to answer", "decline to answer", "prefer not to answer"],
    ["i am not a veteran", "not a veteran", "not a protected veteran"],
    ["i do not have a disability", "no disability", "not disabled"],
]


def _synonyms_for(value_norm: str) -> set:
    out = {value_norm}
    for group in _OPTION_SYNONYMS:
        if value_norm in group:
            out.update(group)
    return out


def _best_option_match(value: str, options: List[str]) -> Optional[str]:
    """Return the option string that best matches *value*, or None.

    Tries, in order: exact, case/punctuation-insensitive, synonym groups,
    substring either-way, then token overlap.
    """
    if not options:
        return None
    vnorm = _norm(value)
    if not vnorm:
        return None
    opt_norm = [(_norm(o), o) for o in options]

    # 1. exact normalized
    for n, o in opt_norm:
        if n == vnorm:
            return o
    # 2. synonyms
    vsyn = _synonyms_for(vnorm)
    for n, o in opt_norm:
        if n in vsyn or _synonyms_for(n) & vsyn:
            return o
    # 3. substring either direction (prefer longest option)
    subs = [o for n, o in opt_norm if n and (n in vnorm or vnorm in n)]
    if subs:
        return max(subs, key=len)
    # 4. token overlap
    vtokens = set(vnorm.split())
    best, best_score = None, 0
    for n, o in opt_norm:
        score = len(vtokens & set(n.split()))
        if score > best_score:
            best, best_score = o, score
    return best


def _humanize(name: str) -> str:
    """Turn a field name/id like 'work_auth.usCitizen' into 'work auth us citizen'."""
    s = re.sub(r"[._\-\[\]]+", " ", str(name or ""))
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return re.sub(r"\s+", " ", s).strip()


_TRUTHY = {"yes", "y", "true", "1", "agree", "i agree", "checked", "check", "on",
           "i certify", "accept", "confirmed"}


async def _fill_text(frame: Any, selector: str, value: str) -> bool:
    """Fill a text/textarea input robustly, dispatching React/Ant events."""
    el = await frame.query_selector(selector)
    if not el or not await el.is_visible():
        return False
    try:
        await el.fill(value)
    except Exception:
        try:
            await el.click()
            await el.fill("")
            await el.type(value, delay=20)
        except Exception:
            return False
    # Make framework-controlled inputs register the change.
    try:
        await el.evaluate(
            """el => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
                    || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                if (setter) setter.call(el, el.value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
            }"""
        )
    except Exception:
        pass
    return True


async def _select_radio_option(frame: Any, options: List[Dict], value: str) -> bool:
    """Click the radio option in a group whose label/value best matches *value*."""
    if not options:
        return False
    labels = [o.get("label") or o.get("value") or "" for o in options]
    match = _best_option_match(value, labels)
    chosen = None
    if match is not None:
        for o in options:
            if (o.get("label") or o.get("value") or "") == match:
                chosen = o
                break
    if chosen is None:
        # Also try matching against option *values* directly.
        vmatch = _best_option_match(value, [o.get("value", "") for o in options])
        for o in options:
            if o.get("value", "") == vmatch:
                chosen = o
                break
    if chosen is None:
        return False
    sel = f"[data-ai-id='{chosen['ai_id']}']" if chosen.get("ai_id") else chosen.get("css_selector")
    try:
        el = await frame.query_selector(sel)
        if not el:
            return False
        try:
            await el.check()
        except Exception:
            await el.click()
        return True
    except Exception:
        return False


async def _apply_checkbox(frame: Any, field: Dict, value: str) -> bool:
    """Check/uncheck a checkbox based on *value* (truthy → checked)."""
    sel = f"[data-ai-id='{field['ai_id']}']" if field.get("ai_id") else field.get("css_selector")
    if not sel:
        return False
    want = _norm(value) in _TRUTHY or "agree" in _norm(value) or "certif" in _norm(value)
    # Consent/agreement checkboxes default to checked when value is unclear.
    label_n = _norm(field.get("label", ""))
    if any(k in label_n for k in ("agree", "consent", "terms", "privacy", "certify", "acknowledge")):
        want = True
    try:
        el = await frame.query_selector(sel)
        if not el or not await el.is_visible():
            return False
        if want:
            await el.check()
        else:
            await el.uncheck()
        return True
    except Exception:
        return False


# ── Page DOM extractor (runs in the browser) ──────────────────────────────────

_FIELD_EXTRACTOR_JS = """
(frameIdx) => {
    let counter = 1;
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();

    function labelFor(el) {
        const lb = el.getAttribute('aria-labelledby');
        if (lb) {
            const t = lb.split(/\\s+/).map(id => {
                const e = document.getElementById(id);
                return e ? e.innerText : '';
            }).join(' ');
            if (norm(t)) return norm(t);
        }
        if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
        if (el.id) {
            try {
                const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (l && norm(l.innerText)) return norm(l.innerText);
            } catch (e) {}
        }
        if (el.labels && el.labels.length) {
            const t = Array.from(el.labels).map(l => l.innerText).join(' ');
            if (norm(t)) return norm(t);
        }
        const wrap = el.closest('label');
        if (wrap && norm(wrap.innerText)) return norm(wrap.innerText);
        const prev = el.previousElementSibling;
        if (prev && norm(prev.innerText)) return norm(prev.innerText);
        if (el.parentElement && norm(el.parentElement.innerText)) return norm(el.parentElement.innerText);
        return '';
    }

    function groupLabelFor(el) {
        const fs = el.closest('fieldset');
        if (fs) {
            const lg = fs.querySelector('legend');
            if (lg && norm(lg.innerText)) return norm(lg.innerText);
        }
        const grp = el.closest('[role="radiogroup"],[role="group"]');
        if (grp) {
            const lb = grp.getAttribute('aria-labelledby');
            if (lb) {
                const e = document.getElementById(lb.split(/\\s+/)[0]);
                if (e && norm(e.innerText)) return norm(e.innerText);
            }
            if (grp.getAttribute('aria-label')) return norm(grp.getAttribute('aria-label'));
        }
        return '';
    }

    function visible(el) {
        if ((el.type || '').toLowerCase() === 'file') return true;
        if (el.tagName.toLowerCase() === 'select') return true;
        return el.offsetParent !== null || (el.getClientRects && el.getClientRects().length > 0);
    }

    const out = [];
    const inputs = Array.from(document.querySelectorAll('input, select, textarea, [role="combobox"], [role="listbox"]'));
    for (const i of inputs) {
        const type = (i.type || i.tagName).toLowerCase();
        if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;

        const aiId = 'ai_' + frameIdx + '_' + (counter++);
        i.setAttribute('data-ai-id', aiId);

        let css = '';
        if (i.id) { try { css = '#' + CSS.escape(i.id); } catch (e) {} }
        if (!css && i.name) { try { css = i.tagName.toLowerCase() + '[name="' + CSS.escape(i.name) + '"]'; } catch (e) {} }

        const lbl = labelFor(i);
        const grp = groupLabelFor(i);

        let isFilled;
        if (type === 'radio' || type === 'checkbox') isFilled = i.checked;
        else isFilled = !!(i.value && i.value.trim() !== '');

        let options = [];
        if (i.tagName.toLowerCase() === 'select') {
            options = Array.from(i.options).map(o => norm(o.innerText))
                .filter(t => t && !t.startsWith('--') && t.toLowerCase() !== 'select' && !/please select|select one|choose/i.test(t));
        }

        const required = i.hasAttribute('required') || i.getAttribute('aria-required') === 'true'
            || (lbl && lbl.includes('*')) || (grp && grp.includes('*'));

        out.push({
            ai_id: aiId,
            css_selector: css,
            frame_idx: frameIdx,
            tag: i.tagName.toLowerCase(),
            type: type,
            role: (i.getAttribute('role') || '').toLowerCase(),
            id: i.id || '',
            name: i.name || '',
            placeholder: i.placeholder || '',
            ariaLabel: i.getAttribute('aria-label') || '',
            label: (lbl || '').substring(0, 200),
            group_label: (grp || '').substring(0, 200),
            option_label: (type === 'radio' || type === 'checkbox') ? (lbl || '').substring(0, 120) : '',
            option_value: (type === 'radio' || type === 'checkbox') ? (i.value || '') : '',
            required: !!required,
            isFilled: isFilled,
            options: options
        });
    }
    return out.filter(i => visible(i) || i.tag === 'select' || i.type === 'file');
}
"""


def _resolve_known_value(field: Dict, candidate: CandidateProfile, page_type: str,
                         email: str, password: str) -> Tuple[Optional[str], Optional[str]]:
    """Deterministic (no-AI) value for a logical field. Returns (value, source) or (None, None)."""
    from .config import SAFE_DEFAULTS
    search_text = " ".join(_norm(field.get(k, "")) for k in ("label", "group_label", "placeholder", "name", "ariaLabel"))

    # 1. Login / register override
    if page_type in ("LOGIN_PAGE", "REGISTER_PAGE", "LOGIN_OR_REGISTER"):
        if "email" in search_text or "username" in search_text:
            return email, "PROFILE"
        if "password" in search_text:
            return password, "PROFILE"

    name = candidate.name or ""
    parts = name.split(" ")
    loc_parts = [p.strip() for p in (candidate.location or "").split(",") if p.strip()]

    if "first name" in search_text and name:
        return parts[0], "INFERRED"
    if ("last name" in search_text or "surname" in search_text or "family name" in search_text) and name:
        return (" ".join(parts[1:]) if len(parts) > 1 else parts[0]), "INFERRED"
    if "full name" in search_text and name:
        return name, "PROFILE"
    if "name" in search_text and name and "company" not in search_text and "user" not in search_text:
        return name, "PROFILE"
    if "email" in search_text and candidate.email:
        return candidate.email, "PROFILE"
    if ("phone" in search_text or "mobile" in search_text or "telephone" in search_text) and candidate.phone:
        return candidate.phone, "PROFILE"
    if "country" in search_text and loc_parts:
        return loc_parts[-1], "INFERRED"
    if "city" in search_text and loc_parts:
        return loc_parts[0], "INFERRED"
    if ("state" in search_text or "province" in search_text) and loc_parts:
        if len(loc_parts) > 2:
            return loc_parts[1], "INFERRED"
        if len(loc_parts) == 2:
            return loc_parts[0], "INFERRED"
    if "years of experience" in search_text and candidate.years_of_experience:
        return candidate.years_of_experience, "PROFILE"
    if "linkedin" in search_text and candidate.linkedin_url:
        return candidate.linkedin_url, "PROFILE"
    if ("website" in search_text or "portfolio" in search_text) and candidate.linkedin_url:
        return candidate.linkedin_url, "INFERRED"

    # SAFE_DEFAULTS (work auth, sponsorship, demographics, etc.)
    for keyword, default_val in SAFE_DEFAULTS.items():
        if keyword in search_text:
            if keyword == "salary":
                if candidate.salary_expectation:
                    return candidate.salary_expectation, "PROFILE"
                return None, None  # leave to AI / blank
            return default_val, "DEFAULT"

    return None, None


async def map_form_fields(page: Page, candidate: CandidateProfile, client: Any, page_type: str, email: str, password: str, emit: Optional[Any] = None, session_id: str = "default") -> List[Dict]:
    """Scan every frame, build a list of *logical* fields, resolve each value via
    deterministic rules first, then resolve all remaining unknowns in ONE batched
    Gemini call. Returns a list of fill instructions for fill_and_submit_mapped_fields."""
    import json

    profile_json = candidate.model_dump_json(exclude={"resume_path"})

    try:
        all_elements: List[Dict] = []
        for frame_idx, frame in enumerate(page.frames):
            try:
                elements = await frame.evaluate(_FIELD_EXTRACTOR_JS, frame_idx)
                if elements:
                    all_elements.extend(elements)
            except Exception as e:
                logger.debug(f"Could not read frame {frame_idx}: {e}")

        if not all_elements:
            return []

        # ── Build logical fields (group radios by name) ───────────────────────
        logical: List[Dict] = []
        radio_groups: Dict[Tuple[int, str], List[Dict]] = {}
        for el in all_elements:
            if el["type"] == "radio" and el["name"]:
                radio_groups.setdefault((el["frame_idx"], el["name"]), []).append(el)
        grouped_ids = {o["ai_id"] for opts in radio_groups.values() for o in opts}

        for (fidx, gname), opts in radio_groups.items():
            label = next((o["group_label"] for o in opts if o["group_label"]), "") or _humanize(gname)
            logical.append({
                "kind": "radio_group",
                "frame_idx": fidx,
                "name": gname,
                "label": label,
                "group_label": label,
                "placeholder": "",
                "ariaLabel": "",
                "required": any(o["required"] for o in opts),
                "isFilled": any(o["isFilled"] for o in opts),
                "options": [
                    {"label": o["option_label"] or o["option_value"], "value": o["option_value"],
                     "ai_id": o["ai_id"], "css_selector": o["css_selector"]}
                    for o in opts
                ],
            })

        for el in all_elements:
            if el["ai_id"] in grouped_ids:
                continue
            tag = el["tag"]
            etype = el["type"]
            role = el.get("role", "")
            if etype == "checkbox":
                kind = "checkbox"
            elif tag == "select" or "combobox" in role or "listbox" in role:
                kind = "select"
            elif tag == "textarea":
                kind = "textarea"
            else:
                kind = "input"
            logical.append({
                "kind": kind,
                "ai_id": el["ai_id"],
                "css_selector": el["css_selector"],
                "frame_idx": el["frame_idx"],
                "tag": tag,
                "type": etype,
                "name": el["name"],
                "label": el["label"],
                "group_label": el["group_label"],
                "placeholder": el["placeholder"],
                "ariaLabel": el["ariaLabel"],
                "required": el["required"],
                "isFilled": el["isFilled"],
                "options": el.get("options", []),
            })

        # ── Resolve deterministic values; collect unknowns for one AI call ────
        results: List[Dict] = []
        unknown: List[Dict] = []
        for f in logical:
            if f.get("isFilled") and f["kind"] != "checkbox":
                continue
            value, source = _resolve_known_value(f, candidate, page_type, email, password)
            if value:
                results.append(_make_fill(f, value, source))
            else:
                unknown.append(f)

        # ── Batched Gemini resolution for everything still unknown ────────────
        if unknown:
            answers = await _batch_ai_answers(unknown, profile_json, client, page, emit, session_id)
            for f in unknown:
                val = answers.get(f.get("ai_id") or f.get("name") or "")
                if val:
                    results.append(_make_fill(f, val, "GEMINI"))

        gemini_n = sum(1 for r in results if r["source"] == "GEMINI")
        logger.info(f"Mapped {len(results)} field(s) ({gemini_n} via AI, {len(logical)} detected).")
        if emit and gemini_n:
            emit({"type": "log", "level": "info", "message": f"AI filled {gemini_n} field(s) in one batch."})
        return results

    except Exception as e:
        logger.error(f"Mapping error: {e}")
        return []


def _make_fill(field: Dict, value: str, source: str) -> Dict:
    return {
        "kind": field["kind"],
        "ai_id": field.get("ai_id"),
        "css_selector": field.get("css_selector"),
        "frame_idx": field.get("frame_idx", 0),
        "value_to_fill": value,
        "label": field.get("label") or field.get("name", ""),
        "source": source,
        "options": field.get("options", []),
    }


async def _batch_ai_answers(unknown: List[Dict], profile_json: str, client: Any,
                            page: Page, emit: Optional[Any], session_id: str) -> Dict[str, str]:
    """Ask Gemini for ALL unknown field values in one structured call.

    Returns {ai_id_or_name: value}. Falls back to per-field calls if the batch
    response can't be parsed. Never raises."""
    import json

    job_title = ""
    try:
        el = await page.query_selector("h1, h2")
        if el:
            job_title = (await el.inner_text())[:120]
    except Exception:
        pass

    spec = []
    for f in unknown:
        key = f.get("ai_id") or f.get("name") or f.get("label")
        entry = {
            "id": key,
            "question": f.get("label") or f.get("group_label") or _humanize(f.get("name", "")),
            "type": f["kind"],
        }
        if f.get("kind") == "radio_group":
            entry["options"] = [o["label"] for o in f.get("options", []) if o.get("label")]
        elif f.get("options"):
            entry["options"] = f["options"]
        spec.append(entry)

    prompt = (
        "You are auto-filling a job application form for a candidate. Use the candidate "
        "profile JSON to answer every field below. Return ONLY a JSON object mapping each "
        "field \"id\" to the best answer string — no markdown, no commentary.\n"
        "Rules: pick the safest answer that will NOT disqualify the candidate. For "
        "'radio_group' or 'select' fields, return EXACTLY one of the provided options "
        "verbatim. For 'textarea', write a concise 2-3 sentence professional answer "
        "specific to the candidate. For yes/no questions not covered by the profile, "
        "prefer the answer that keeps the candidate eligible. Never return an empty string.\n"
        f"Job title on page: {job_title}\n"
        f"Candidate profile JSON: {profile_json}\n"
        f"Fields (JSON array): {json.dumps(spec, ensure_ascii=False)}"
    )

    try:
        resp = await _call_gemini_with_retry(client, prompt)
        text = (resp.text or "").strip()
        start, end = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[start:end])
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items() if v not in (None, "")}
    except Exception as e:
        logger.warning(f"Batch AI answer failed ({e}); falling back to per-field.")

    # ── Fallback: per-field (now bug-free) ────────────────────────────────────
    out: Dict[str, str] = {}
    for f in unknown:
        key = f.get("ai_id") or f.get("name") or f.get("label")
        question = f.get("label") or f.get("group_label") or _humanize(f.get("name", ""))
        opts = entry_opts = ([o["label"] for o in f.get("options", []) if o.get("label")]
                             if f.get("kind") == "radio_group" else f.get("options", []))
        if f.get("kind") == "textarea":
            p = (f"Write a concise 2-3 sentence professional answer for the job application "
                 f"field '{question}'. Candidate profile JSON: {profile_json}.")
        else:
            p = (f"Job application field: '{question}'. Available options: {opts}. "
                 f"Candidate profile JSON: {profile_json}. Return ONLY the value to fill "
                 f"(for a dropdown/radio return exactly one option verbatim). Pick the "
                 f"safest non-disqualifying answer. Never return empty.")
        try:
            r = await _call_gemini_with_retry(client, p)
            v = (r.text or "").strip()
            if v:
                out[str(key)] = v
        except Exception as e:
            logger.debug(f"Per-field AI failed for '{question}': {e}")
    return out

async def fill_and_submit_mapped_fields(page: Page, fields: List[Dict], candidate: CandidateProfile, client: Any, emit: Optional[Any] = None, session_id: str = "default") -> int:
    from .browser import human_delay
    filled = 0
    for field in fields:
        await check_pause_state(session_id)
        kind = field.get("kind", "input")
        val = field.get("value_to_fill")
        label = field.get("label", "")
        frame_idx = field.get("frame_idx", 0)
        source = field.get("source", "default")

        if val is None and kind != "checkbox":
            continue

        target_frame = page.frames[frame_idx] if frame_idx < len(page.frames) else page
        ok = False
        try:
            if kind == "radio_group":
                ok = await _select_radio_option(target_frame, field.get("options", []), str(val))
            elif kind == "checkbox":
                ok = await _apply_checkbox(target_frame, field, str(val) if val is not None else "")
            elif kind == "select":
                sel = f"[data-ai-id='{field['ai_id']}']" if field.get("ai_id") else field.get("css_selector")
                ok = await fill_dropdown(target_frame, sel, str(val), field.get("options"))
            else:  # input / textarea
                sel = f"[data-ai-id='{field['ai_id']}']" if field.get("ai_id") else field.get("css_selector")
                ok = await _fill_text(target_frame, sel, str(val))
                if not ok and field.get("css_selector"):
                    ok = await _fill_text(target_frame, field["css_selector"], str(val))
        except Exception as e:
            logger.debug(f"Failed to fill '{label}' ({kind}) in frame {frame_idx}: {e}")

        if ok:
            filled += 1
            await human_delay(0.2, 0.5)
            if emit:
                emit({"type": "log", "level": "info", "message": f"Field filled: {label} = {val} (source: {source})"})

    return filled

async def click_next_or_submit(page: Page) -> bool:
    from .browser import human_delay
    btn_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Submit')",
        "a:has-text('Next')",
        "a:has-text('Continue')",
        "button:has-text('Apply')"
    ]
    for sel in btn_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible() and await btn.is_enabled():
                await btn.click()
                await human_delay(2.0, 4.0)
                logger.info(f"Clicked navigation button: {sel}")
                return True
        except Exception:
            pass
    return False
