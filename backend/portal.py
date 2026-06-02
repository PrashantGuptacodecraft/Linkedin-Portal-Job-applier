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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import asyncio
from loguru import logger
import time

async def _call_gemini_with_retry(client, prompt: str, retries: int = 4) -> Any:
    """Wrapper around Gemini API calls to handle 429 Rate Limit and 503 errors."""
    for attempt in range(retries):
        try:
            if hasattr(client, 'generate_content_async'):
                response = await client.generate_content_async(prompt)
            else:
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(None, client.generate_content, prompt)
            return response
        except Exception as e:
            err_str = str(e)
            if attempt < retries - 1:
                wait_time = 5.0
                if "429" in err_str or "Quota exceeded" in err_str:
                    wait_time = 40.0
                    import re
                    match = re.search(r'retry in ([0-9.]+)s', err_str)
                    if match:
                        wait_time = float(match.group(1)) + 1.0
                logger.warning(f"Gemini API Error: {err_str[:100]}... Waiting {wait_time:.1f}s before retry {attempt+1}/{retries}...")
                await asyncio.sleep(wait_time)
            else:
                logger.error("Gemini API max retries exceeded.")
                raise e


from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

try:
    from .browser import goto_with_retries, human_delay
    from .models import CandidateProfile
except ImportError:
    from browser import goto_with_retries, human_delay
    from models import CandidateProfile


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
                    if emit:
                        emit({"type": "manual_login_required", "message": "Cloudflare verification required on this portal — please solve it manually in the browser window. Bot will continue automatically once cleared."})
                    logger.info("Cloudflare still blocking. Emitted manual solve event, waiting up to 120s.")
                    try:
                        await page.wait_for_function("document.title !== 'Just a moment...' && !document.title.toLowerCase().includes('verifying')", timeout=120000)
                        await human_delay(1.5, 2.5)
                        logger.info("Manual Cloudflare solve detected. Continuing flow.")
                    except Exception:
                        logger.warning("Cloudflare manual solve timeout.")
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

        # ── Layer 7: AI fallback (only if semantic fill got very few fields)
        if filled < 2:
            logger.info(f"Semantic fill got only {filled} field(s). Trying AI fallback...")
            if emit:
                emit({"type": "log", "level": "info", "message": "Initializing AI-powered application flow..."})
            try:
                ai_filled = await _ai_fallback_handler(page, candidate, login_credentials, emit)
                filled += ai_filled
            except Exception as exc:
                logger.error(f"AI fallback handler crashed: {exc}")
                if emit:
                    emit({"type": "log", "level": "warning", "message": f"AI handler error: {exc}"})
        else:
            logger.info(f"Semantic fill sufficient ({filled} fields). Skipping AI handler.")
            if emit:
                emit({"type": "log", "level": "info", "message": f"Filled {filled} field(s) via semantic matching."})

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


# ── Layer 3 – Resume upload ───────────────────────────────────────────────────

async def _upload_resume(page: Page, resume_path: str) -> int:
    """
    Find any file-input element and set the resume file path.
    Returns 1 if an upload was performed, else 0.
    """
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

async def _ai_fallback_handler(page: Page, candidate: CandidateProfile, login_credentials: Optional[Any], emit: Optional[Any] = None) -> int:
    from .config import GEMINI_API_KEY, get_portal_credentials
    from .browser import human_delay
    from pathlib import Path
    import asyncio
    import json
    
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set. Cannot run Universal AI Handler.")
        return 0
        
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    client = genai.GenerativeModel("gemini-2.0-flash")
    
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
        elif page_type == "UNKNOWN":
            logger.info("AI could not understand the page. Stopping.")
            break
        elif page_type in ("LOGIN_PAGE", "REGISTER_PAGE", "LOGIN_OR_REGISTER"):
            creds = get_portal_credentials(page.url, login_credentials)
            email = creds.get("email")
            password = creds.get("password")
            
            if not email or not password:
                logger.warning("Missing portal credentials for login/register.")
                break
                
            fields = await map_form_fields(page, candidate, client, page_type, email, password, emit)
            if not fields:
                logger.warning("No fields mapped for login/register.")
                break
                
            filled_count += await fill_and_submit_mapped_fields(page, fields, candidate, client, emit)
            
        elif page_type == "APPLICATION_FORM":
            # Upgrade 9: Form Before Screenshot
            try:
                from .diagnostics import DIAGNOSTICS_DIR
                await page.screenshot(path=str(DIAGNOSTICS_DIR / "form_before.png"), full_page=True)
                if emit: emit({"type": "log", "level": "info", "message": "Screenshot saved: form_before.png"})
            except Exception as e:
                logger.debug(f"Failed to capture form_before.png: {e}")

            fields = await map_form_fields(page, candidate, client, page_type, "", "", emit)
            if fields:
                filled_count += await fill_and_submit_mapped_fields(page, fields, candidate, client, emit)
            else:
                logger.info("No application fields found to fill. Attempting to upload resume.")
                if candidate.resume_path:
                    filled_count += await _upload_resume(page, candidate.resume_path, emit)
                    
            # Upgrade 6: Pre-submit validation
            await _pre_submit_validation(page, candidate, client, emit)

            # Upgrade 9: Form After Screenshot
            try:
                from .diagnostics import DIAGNOSTICS_DIR
                await page.screenshot(path=str(DIAGNOSTICS_DIR / "form_after.png"), full_page=True)
                if emit: emit({"type": "log", "level": "info", "message": "Screenshot saved: form_after.png"})
            except Exception as e:
                logger.debug(f"Failed to capture form_after.png: {e}")
                
            # Upgrade 5: reCAPTCHA detection
            await _handle_recaptcha_before_submit(page, emit)

            # Try to click next/continue/submit
            clicked = await click_next_or_submit(page)
            if not clicked:
                logger.info("No 'Next' or 'Submit' button found. Form may be complete.")
                break
                
            # Upgrade 7: Post-submit confirmation
            await _post_submit_confirmation(page, emit)
    
    return filled_count

async def ai_answer_unknown_field(field_label: str, field_type: str, candidate: CandidateProfile, api_key: str) -> str:
    """Upgrade 2: AI-powered field answering for unknown questions using Gemini."""
    import google.generativeai as genai
    import json
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")
    
    profile_json = candidate.model_dump_json(exclude={'resume_path'})
    prompt_string = f"""You are filling a job application form on behalf of a candidate. The field label is: {field_label}. The field type is: {field_type}. Here is everything known about the candidate as JSON: {profile_json}. Based on this, what is the most appropriate answer for this field? Rules: if the candidate profile does not contain a direct answer, use the safest most common answer that would not disqualify them. For yes/no questions about things not in the profile such as security clearance, default to No. For location preference, use the candidate's stored preferred_location or location field. For citizenship and work authorization questions, use the work_authorization field. For questions about salary, use salary_expectation field or leave blank if empty. Return ONLY the answer value as a plain string with absolutely no explanation, no punctuation around it, no quotes."""
    
    try:
        response = await _call_gemini_with_retry(model, prompt_string)
        return response.text.strip()
    except Exception as e:
        logger.error(f"ai_answer_unknown_field failed for {field_label}: {e}")
        return ""

async def fill_dropdown(page: Page, selector: str, desired_value: str):
    """Upgrade 3: Smart dropdown handler."""
    from .browser import human_delay
    try:
        el = await page.query_selector(selector)
        if not el: return
        tag = await el.evaluate("e => e.tagName.toLowerCase()")
        role = await el.evaluate("e => e.getAttribute('role') || ''")
        
        # Type 1: Native HTML select
        if tag == 'select':
            options = await el.evaluate("""e => Array.from(e.options).map(o => ({text: o.innerText, value: o.value}))""")
            best_match = None
            for opt in options:
                if desired_value.lower() in opt['text'].lower():
                    best_match = opt['text']
                    break
            if best_match:
                await el.select_option(label=best_match)
            return

        # Type 2: Custom div-based dropdown (role=listbox or combobox, not input)
        if tag != 'input' and ('listbox' in role.lower() or 'combobox' in role.lower()):
            await el.click()
            await human_delay(0.5, 0.5)
            # Find visible options
            options = await page.query_selector_all("li, [role='option'], .option, .item")
            for opt in options:
                if await opt.is_visible():
                    text = await opt.inner_text()
                    if text and desired_value.lower() in text.lower():
                        await opt.click()
                        return
            return

        # Type 3: Combobox text input
        if tag == 'input' and 'combobox' in role.lower():
            await el.click()
            await el.fill("") # Clear first
            for char in desired_value:
                await el.type(char, delay=50)
            await human_delay(0.8, 0.8)
            # Click first suggestion
            options = await page.query_selector_all("li, [role='option']")
            for opt in options:
                if await opt.is_visible():
                    await opt.click()
                    return
    except Exception as e:
        logger.error(f"fill_dropdown failed for {selector}: {e}")

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

async def _pre_submit_validation(page: Page, candidate: CandidateProfile, client: Any, emit: Optional[Any]):
    """Upgrade 6: Pre-submit required field validation."""
    from .config import GEMINI_API_KEY
    from .browser import human_delay
    try:
        required_elements = []
        for frame_idx, frame in enumerate(page.frames):
            try:
                frame_reqs = await frame.evaluate("""
                (frameIdx) => {
                    const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
                    return inputs.map(i => {
                        const isRequired = i.hasAttribute('required') || i.getAttribute('aria-required') === 'true' || 
                                           (i.labels && i.labels.length > 0 && i.labels[0].innerText.includes('*'));
                        const val = i.value || '';
                        const emptyVals = ['-- No answer --', '-- Select --', '-- Please select --', ''];
                        if (isRequired && emptyVals.includes(val)) {
                            const aiId = i.getAttribute('data-ai-id');
                            return {
                                css_selector: aiId ? `[data-ai-id='${aiId}']` : (i.id ? '#' + i.id : (i.name ? '[name="' + i.name + '"]' : '')),
                                frame_idx: frameIdx,
                                label: (i.labels && i.labels.length > 0) ? i.labels[0].innerText : (i.getAttribute('aria-label') || ''),
                                type: i.tagName.toLowerCase() === 'select' ? 'dropdown' : i.type
                            };
                        }
                        return null;
                    }).filter(i => i !== null && i.css_selector !== '');
                }
                """, frame_idx)
                if frame_reqs: required_elements.extend(frame_reqs)
            except Exception:
                pass
        
        for req in required_elements:
            if not req.get('label'): continue
            ans = await ai_answer_unknown_field(req['label'], req['type'], candidate, GEMINI_API_KEY)
            if ans:
                frame_idx = req.get('frame_idx', 0)
                target_frame = page.frames[frame_idx] if frame_idx < len(page.frames) else page
                if req['type'] == 'dropdown':
                    await fill_dropdown(target_frame, req['css_selector'], ans)
                else:
                    el = await target_frame.query_selector(req['css_selector'])
                    if el: await el.fill(ans)
                if emit: emit({"type": "log", "level": "info", "message": f"Pre-submit fix: filled {req['label']} with {ans}"})
                await human_delay(0.5, 1.0)
    except Exception as e:
        logger.error(f"Pre-submit validation failed: {e}")

async def _handle_recaptcha_before_submit(page: Page, emit: Optional[Any]):
    """Upgrade 5: reCAPTCHA detection and handling."""
    import asyncio
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
                if emit: emit({"type": "manual_login_required", "message": "reCAPTCHA image challenge detected — please solve manually in the browser window"})
                for _ in range(18):
                    await asyncio.sleep(10)
                    still_there = await page.query_selector('iframe[title*="challenge"], iframe[src*="bframe"]')
                    if not still_there:
                        break
    except Exception as e:
        logger.error(f"reCAPTCHA handling error: {e}")

async def _post_submit_confirmation(page: Page, emit: Optional[Any]):
    """Upgrade 7: Post-submit confirmation detection."""
    from .models import JobStatus
    import asyncio
    try:
        success_indicators = ["confirmation", "thank-you", "thank_you", "success", "submitted", "complete", "apply-complete"]
        success_phrases = ["application submitted", "thank you for applying", "we have received your application", "application complete", "you have successfully applied", "your application has been received"]
        
        found_success = False
        for _ in range(7):
            await asyncio.sleep(2)
            url = page.url.lower()
            if any(ind in url for ind in success_indicators):
                found_success = True
                break
            
            body = await page.inner_text('body')
            body_lower = body.lower()
            if any(phrase in body_lower for phrase in success_phrases):
                found_success = True
                break
                
            dialog = await page.query_selector('[role="dialog"]')
            if dialog:
                found_success = True
                break
                
        if found_success:
            if emit: emit({"type": "log", "level": "info", "message": "Application submitted successfully — confirmation detected"})
            # To mark as APPLIED, we don't return JobStatus directly here, orchestrator sets it based on filled_count.
        else:
            from .diagnostics import DIAGNOSTICS_DIR
            await page.screenshot(path=str(DIAGNOSTICS_DIR / "submit_result.png"), full_page=True)
            if emit: emit({"type": "log", "level": "warning", "message": "Submit clicked but no confirmation detected — screenshot saved for manual review"})
            # The orchestrator is listening, but we can't easily force MANUAL_REVIEW from here unless we raise an exception or modify the return tuple. We will rely on orchestrator observing filled>0 as APPLIED for now, or if it catches MANUAL_REVIEW if we raise a specific error. The user said "mark job status as MANUAL_REVIEW rather than FAILED". We'll just return a special count or rely on the orchestrator to check state. Let's just raise an Exception so orchestrator can catch it, wait no, they said "mark job status as MANUAL_REVIEW". We will raise an exception: `raise Exception("MANUAL_REVIEW: No confirmation detected")`
            raise Exception("MANUAL_REVIEW: Submit clicked but no confirmation detected")
    except Exception as e:
        if "MANUAL_REVIEW" in str(e):
            raise
        logger.error(f"Post-submit check error: {e}")

async def classify_portal_page(page: Page, client: Any) -> Tuple[str, str]:
    import json
    try:
        html = await page.evaluate("document.body.innerText")
        content = html[:10000]
        
        prompt = f"""Analyze the following web page text and classify its primary purpose.
        Respond in JSON with 'page_type' and 'reasoning'.
        Valid page_types: LOGIN_PAGE, REGISTER_PAGE, LOGIN_OR_REGISTER, APPLICATION_FORM, CAPTCHA, UNKNOWN.
        
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
        return "UNKNOWN", str(e)

async def map_form_fields(page: Page, candidate: CandidateProfile, client: Any, page_type: str, email: str, password: str, emit: Optional[Any] = None) -> List[Dict]:
    import json
    from .config import GEMINI_API_KEY
    try:
        all_elements = []
        for frame_idx, frame in enumerate(page.frames):
            try:
                elements = await frame.evaluate("""
                (frameIdx) => {
                    let counter = 1;
                    const inputs = Array.from(document.querySelectorAll('input, select, textarea, [role="combobox"], [role="listbox"]'));
                    return inputs.map(i => {
                        const aiId = 'ai_' + frameIdx + '_' + counter++;
                        i.setAttribute('data-ai-id', aiId);
                        
                        let labelText = '';
                        if (i.labels && i.labels.length > 0) {
                            labelText = i.labels[0].innerText;
                        } else {
                            const prev = i.previousElementSibling;
                            if (prev && prev.innerText) labelText = prev.innerText;
                            else if (i.parentElement && i.parentElement.innerText) {
                                labelText = i.parentElement.innerText.replace(i.innerText || '', '').trim();
                            }
                        }
                        
                        let cssSelector = '';
                        if (i.id) {
                            try { cssSelector = '#' + CSS.escape(i.id); } catch(e) {}
                        }
                        if (!cssSelector && i.name) {
                            try { cssSelector = i.tagName.toLowerCase() + '[name="' + CSS.escape(i.name) + '"]'; } catch(e) {}
                        }
                        
                        return {
                            ai_id: aiId,
                            css_selector: cssSelector,
                            frame_idx: frameIdx,
                            tag: i.tagName.toLowerCase(),
                            type: i.type || '',
                            id: i.id || '',
                            name: i.name || '',
                            placeholder: i.placeholder || '',
                            ariaLabel: i.getAttribute('aria-label') || '',
                            label: labelText.substring(0, 150)
                        };
                    }).filter(i => i.type !== 'hidden' && i.type !== 'submit');
                }
                """, frame_idx)
                
                if elements:
                    all_elements.extend(elements)
            except Exception as e:
                logger.debug(f"Could not read frame {frame_idx}: {e}")
        
        if not all_elements:
            return []
            
        profile_json = candidate.model_dump_json(exclude={'resume_path'})
        
        prompt = f"""Map the provided form fields to the candidate profile data.
        Page Type: {page_type}
        Candidate Data: {profile_json}
        Portal Email: {email}
        Portal Password: {password}
        Form Elements: {json.dumps(all_elements)}
        
        Return a JSON array where each object has 'ai_id', 'frame_idx', 'value_to_fill', 'label', and 'source'.
        'source' must be exactly one of: 'profile_direct', 'profile_inferred', 'default_value', 'unknown'. Use 'unknown' if no profile key matches.
        For select dropdowns, provide the text value to select.
        If it's a login/register page, strictly map the Portal Email and Portal Password to the correct fields.
        Ignore file uploads or irrelevant fields.
        """
        
        response = await _call_gemini_with_retry(client, prompt)
        text = response.text.strip()
        start = text.find('[')
        end = text.rfind(']') + 1
        if start != -1 and end != -1:
            data = json.loads(text[start:end])
            
            ai_id_to_css = {el['ai_id']: el.get('css_selector', '') for el in all_elements}
            
            for item in data:
                item['css_selector'] = ai_id_to_css.get(item.get('ai_id'), '')
                if item.get('source') == 'unknown':
                    ans = await ai_answer_unknown_field(item.get('label', ''), "text", candidate, GEMINI_API_KEY)
                    if ans:
                        item['value_to_fill'] = ans
                        item['source'] = 'ai_answered'
                        
            return data
        return []
    except Exception as e:
        logger.error(f"Mapping error: {e}")
        return []

async def fill_and_submit_mapped_fields(page: Page, fields: List[Dict], candidate: CandidateProfile, client: Any, emit: Optional[Any] = None) -> int:
    from .browser import human_delay
    filled = 0
    for field in fields:
        ai_id = field.get('ai_id')
        sel = f"[data-ai-id='{ai_id}']" if ai_id else field.get('css_selector')
        val = field.get('value_to_fill')
        source = field.get('source', 'default_value')
        label = field.get('label', sel)
        frame_idx = field.get('frame_idx', 0)
        
        if not sel or not val:
            continue
            
        try:
            target_frame = page.frames[frame_idx] if frame_idx < len(page.frames) else page
            el = await target_frame.query_selector(sel)
            
            if not el and field.get('css_selector'):
                try:
                    el = await target_frame.query_selector(field.get('css_selector'))
                except Exception:
                    pass
            
            if el and await el.is_visible():
                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                role = await el.evaluate("e => e.getAttribute('role') || ''")
                
                if tag == 'select' or 'listbox' in role.lower() or 'combobox' in role.lower():
                    await fill_dropdown(target_frame, sel, str(val))
                else:
                    await el.fill(str(val))
                await human_delay(0.2, 0.5)
                filled += 1
                
                if emit: emit({"type": "log", "level": "info", "message": f"Field filled: {label} = {val} (source: {source})"})
        except Exception as e:
            logger.debug(f"Failed to fill {sel} in frame {frame_idx}: {e}")
            
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
