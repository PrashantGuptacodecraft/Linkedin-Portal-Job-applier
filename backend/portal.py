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

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from browser import human_delay
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
    context:   BrowserContext,
    apply_url: str,
    candidate: CandidateProfile,
) -> Tuple[Page, int, Optional[str]]:
    """
    Open *apply_url* in a new tab, autofill the application form, and
    attempt to upload the resume.

    Returns (page, filled_field_count, portal_state).
    The caller is responsible for closing the page after saving diagnostics.
    """
    page = await context.new_page()
    try:
        await page.goto(apply_url, timeout=35_000, wait_until="domcontentloaded")
        await human_delay(2.5, 4.0)
    except PWTimeout:
        logger.warning(f"Portal page load timed out: {apply_url}")

    portal_state = await detect_portal_state(page)
    if portal_state:
        logger.info(f"Portal state detected: {portal_state}")

    filled = 0

    # 1. Try known-portal adapters
    for pattern, adapter in _PORTAL_ADAPTERS:
        if pattern.search(apply_url) or pattern.search(page.url):
            logger.info(f"Using portal adapter: {adapter.__name__}")
            filled += await adapter(page, candidate)
            break   # one adapter is enough for the specialist logic

    # 2. Semantic fill (runs on every portal to catch remaining fields)
    filled += await _semantic_fill(page, candidate)

    # 3. Upload resume
    if candidate.resume_path and Path(candidate.resume_path).exists():
        filled += await _upload_resume(page, candidate.resume_path)

    logger.info(f"Portal autofill complete – {filled} field(s) filled.")
    return page, filled, portal_state


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
    captcha_markers = ["captcha", "checkpoint", "challenge", "verify you are human", "security verification"]
    # Treat known LinkedIn safety/interstitial pages as captcha/challenge walls
    if ("linkedin.com" in url and ("/safety/" in url or "safety/go" in url or "checkpoint" in url)):
        return "captcha_required"

    if any(marker in url or marker in title for marker in captcha_markers):
        return "captcha_required"

    # If application-form fields are visible, the page is likely usable
    try:
        form_fields = await page.query_selector_all(
            "input[type='text'], input[type='email'], input[type='tel'], textarea"
        )
        visible_fields = 0
        for f in form_fields[:10]:  # cap iteration
            try:
                if await f.is_visible():
                    visible_fields += 1
            except Exception:
                pass
        if visible_fields >= 2:
            return None  # looks like a real form — not a login wall
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

    # Also check the full body for captcha markers only
    try:
        body = ((await page.text_content("body")) or "").lower()
    except Exception:
        body = ""

    if any(marker in body for marker in captcha_markers):
        return "captcha_required"

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
    return {
        # full name variants
        "full name":    c.name,
        "full_name":    c.name,
        "name":         c.name,
        # first / last name
        "first name":   name_parts[0],
        "first_name":   name_parts[0],
        "firstname":    name_parts[0],
        "last name":    name_parts[1] if len(name_parts) > 1 else "",
        "last_name":    name_parts[1] if len(name_parts) > 1 else "",
        "lastname":     name_parts[1] if len(name_parts) > 1 else "",
        # contact
        "email":        c.email,
        "email address": c.email,
        "phone":        c.phone,
        "phone number": c.phone,
        "mobile":       c.phone,
        "mobile number": c.phone,
        # location
        "location":     c.location or "",
        "city":         (c.location or "").split(",")[0].strip(),
        # cover letter
        "cover letter": c.cover_text,
        "cover_letter": c.cover_text,
        "message":      c.cover_text,
        "additional information": c.cover_text,
        # generated-resume fields
        "skills":       getattr(c, "technical_skills", "") or "",
        "technical skills": getattr(c, "technical_skills", "") or "",
        "projects":     getattr(c, "projects", "") or "",
        "project":      getattr(c, "projects", "") or "",
        "target role":  getattr(c, "target_role", "") or "",
        # linkedin
        "linkedin":     c.linkedin_url or "",
        "linkedin url": c.linkedin_url or "",
        "linkedin profile": c.linkedin_url or "",
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
        "input[type='file'][accept*='pdf']",
        "input[type='file'][accept*='.doc']",
        "input[type='file']",
        "[aria-label*='resume' i] input[type='file']",
        "[aria-label*='cv' i] input[type='file']",
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
