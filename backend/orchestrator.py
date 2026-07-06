"""
orchestrator.py – Central pipeline that drives one full automation run.

Flow
----
1.  Validate inputs.
2.  Launch browser / restore session.
3.  Ensure LinkedIn login (manual or automated).
4.  Collect job URLs (search or single).
5.  For each URL:
        a. Extract job details (title, company, apply URL).
        b. Skip if Easy Apply or no external URL.
        c. Call portal autofill.
        d. Capture diagnostics on any failure.
        e. Emit SSE events so the UI stays live.
6.  Tear down browser, save session.
7.  Return SessionSummary.
"""

from __future__ import annotations

import asyncio
import threading
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
# Prefer Patchright's driver (patched, undetected) when installed; the Python API
# is identical, so the rest of the code is unchanged. Error classes are shared
# with playwright, so PWTimeout still matches raised timeouts.
try:
    from patchright.async_api import async_playwright
    from playwright.async_api import TimeoutError as PWTimeout
except ImportError:
    from playwright.async_api import TimeoutError as PWTimeout, async_playwright

try:
    from .browser  import build_context, goto_with_retries, human_delay, save_session, new_stealth_page
    from .config   import DIAGNOSTICS_DIR, DomainSkippedError
    from .diagnostics import DiagnosticsCapture
    from .linkedin import (
        build_search_url,
        build_search_urls,
        discover_job_cards,
        ensure_logged_in,
        extract_job_details,
        open_external_apply_page,
    )
    from .models   import AutoApplyRequest, JobResult, JobStatus, SessionSummary
    from .portal   import autofill_portal_form
except ImportError:
    from browser  import build_context, goto_with_retries, human_delay, save_session, new_stealth_page
    from config   import DIAGNOSTICS_DIR, DomainSkippedError
    from diagnostics import DiagnosticsCapture
    from linkedin import (
        build_search_url,
        build_search_urls,
        discover_job_cards,
        ensure_logged_in,
        extract_job_details,
    )
    from models   import AutoApplyRequest, JobResult, JobStatus, SessionSummary
    from portal   import autofill_portal_form


# ── Public entry point ────────────────────────────────────────────────────────

async def run_pipeline(
    req: AutoApplyRequest,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    *,
    apply_external: bool = True,
    stop_event: Optional[threading.Event] = None,
    session_id: Optional[str] = None,
) -> SessionSummary:
    """
    Run the full automation pipeline.

    *emit* is a synchronous callback called with SSE event dicts.
    It is safe to call from any async context (the caller queues them).
    """
    if not session_id:
        session_id = str(uuid.uuid4())[:8]
    summary    = SessionSummary(session_id=session_id)
    console_buf: list = []

    def emit_event(event: Dict[str, Any]) -> None:
        event["session_id"] = session_id
        summary.logs.append(event)
        if emit:
            try:
                emit(event)
            except Exception as exc:
                logger.warning(f"emit_event callback error: {exc}")

    emit_event({"type": "log", "level": "info", "message": f"Session {session_id} starting …"})

    try:
        async with async_playwright() as pw:
            emit_event({"type": "log", "level": "info", "message": "Launching browser…"})
            browser, context = await build_context(
                pw,
                headless=req.headless,
                console_buf=console_buf,
                record_har=True,
                session_id=session_id,
            )
            diag = DiagnosticsCapture(console_buf)
            emit_event({"type": "log", "level": "info", "message": "Browser launched successfully."})

            try:
                # ── 1. LinkedIn login ─────────────────────────────────────────
                logged_in = await ensure_logged_in(
                    context,
                    manual=not req.headless,  # always show browser for 2FA
                    linkedin_email=req.login_credentials.linkedin_email,
                    linkedin_password=req.login_credentials.linkedin_password,
                    emit=emit_event,
                )
                if not logged_in:
                    emit_event({"type": "error", "message": "LinkedIn login failed. Aborting."})
                    return summary

                # -------------------------------------------------------------
                # Branch: C2C Post Application Mode
                # -------------------------------------------------------------
                search_in = req.filters.extra_params.get('search_in') if req.filters else None
                if search_in == 'linkedin_posts':
                    emit_event({"type": "log", "level": "info", "message": "Executing C2C LinkedIn Posts pipeline..."})
                    from .linkedin import build_search_url, discover_post_leads
                    from .gmail_client import send_c2c_application_email
                    from .config import GEMINI_API_KEY
                    
                    search_url = req.search_url or build_search_url(req.keywords, filters=req.filters)
                    summary.search_url = search_url
                    
                    emit_event({"type": "log", "level": "info", "message": f"Navigating to feed: {search_url}"})
                    page = await new_stealth_page(context)
                    try:
                        await goto_with_retries(page, search_url, timeout=30_000)
                        await human_delay(2.5, 4.0)
                        
                        emit_event({"type": "log", "level": "info", "message": "Scrolling feed and extracting recruiter emails..."})
                        leads = await discover_post_leads(page, req.max_jobs)
                        
                        if not leads:
                            emit_event({"type": "log", "level": "warning", "message": "No recruiter emails found in posts."})
                            return summary
                            
                        summary.total = len(leads)
                        emit_event({"type": "log", "level": "info", "message": f"Found {len(leads)} C2C leads."})
                        
                        for lead in leads:
                            if stop_event and stop_event.is_set(): break
                            
                            emit_event({"type": "status", "job_url": lead['url'], "status": "processing"})
                            emit_event({"type": "log", "level": "info", "message": f"Sending C2C email to {lead['email']}"})
                            
                            success = await send_c2c_application_email(
                                to_email=lead['email'],
                                post_text=lead['text'],
                                candidate=req.candidate_profile,
                                resume_path=req.candidate_profile.resume_path,
                                credentials=req.login_credentials,
                                gemini_api_key=GEMINI_API_KEY
                            )
                            
                            if success:
                                summary.applied += 1
                                emit_event({"type": "status", "job_url": lead['url'], "status": "applied"})
                                emit_event({"type": "log", "level": "success", "message": f"Applied to {lead['author']} via Gmail."})
                            else:
                                summary.failed += 1
                                emit_event({"type": "status", "job_url": lead['url'], "status": "failed"})
                                
                            await human_delay(3.0, 6.0)
                            
                    finally:
                        await page.close()
                    
                    return summary


                emit_event({"type": "log", "level": "info", "message": "LinkedIn login confirmed."})

                # ── 2. Collect job URLs ───────────────────────────────────────
                job_urls = await _collect_job_urls(req, context, emit_event, summary)
                if not job_urls:
                    emit_event({"type": "log", "level": "warning", "message": "No job URLs found. Check keywords or search URL."})
                    return summary

                summary.total = len(job_urls)
                emit_event({"type": "log", "level": "info", "message": f"Found {len(job_urls)} job(s) to process."})

                # ── 3. Process each job ───────────────────────────────────────
                detail_page = await new_stealth_page(context)
                retry_queue: List[str] = []
                retried_jobs: set[str] = set()

                for idx, job_url in enumerate(job_urls, start=1):
                    if stop_event and stop_event.is_set():
                        logger.info("Pipeline cancelled by client.")
                        emit_event({"type": "info", "message": "Pipeline stopped by user."})
                        break

                    result = await _process_one_job(
                        idx         = idx,
                        total       = len(job_urls),
                        job_url     = job_url,
                        req         = req,
                        context     = context,
                        detail_page = detail_page,
                        diag        = diag,
                        emit        = emit_event,
                        apply_external = apply_external,
                        session_id  = session_id,
                    )

                    if result.error == "captcha_required" and job_url not in retried_jobs:
                        retry_queue.append(job_url)
                        retried_jobs.add(job_url)
                        emit_event({"type": "log", "level": "warning", "message": f"Verification wall detected for {job_url}; requeueing once after manual solve."})
                        continue

                    summary.results.append(result)

                    if result.status == JobStatus.APPLIED:
                        summary.applied += 1
                    elif result.status == JobStatus.SKIPPED:
                        summary.skipped += 1
                    elif result.status == JobStatus.MANUAL_REVIEW:
                        summary.needs_review += 1
                    else:
                        summary.failed  += 1

                    emit_event({"type": "job_result", "job": result.model_dump(mode="json")})
                    await human_delay(2.0, 5.0)

                for job_url in retry_queue:
                    result = await _process_one_job(
                        idx         = len(summary.results) + 1,
                        total       = len(job_urls),
                        job_url     = job_url,
                        req         = req,
                        context     = context,
                        detail_page = detail_page,
                        diag        = diag,
                        emit        = emit_event,
                        apply_external = apply_external,
                        session_id  = session_id,
                    )
                    summary.results.append(result)
                    if result.status == JobStatus.APPLIED:
                        summary.applied += 1
                    elif result.status == JobStatus.SKIPPED:
                        summary.skipped += 1
                    elif result.status == JobStatus.MANUAL_REVIEW:
                        summary.needs_review += 1
                    else:
                        summary.failed += 1
                    emit_event({"type": "job_result", "job": result.model_dump(mode="json")})
                    await human_delay(2.0, 5.0)

                await detail_page.close()

            except Exception as exc:
                logger.error(f"Pipeline error: {exc}\n{traceback.format_exc()}")
                emit_event({"type": "error", "message": f"Pipeline crashed: {exc}"})

            finally:
                try:
                    await save_session(context)
                except Exception as exc:
                    logger.debug(f"save_session failed in teardown: {exc}")
                # Closing the context also flushes the HAR file
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

    except Exception as exc:
        # This catches Playwright launch failures, browser crashes, etc.
        logger.error(f"Browser/Playwright error: {exc}\n{traceback.format_exc()}")
        emit_event({"type": "error", "message": f"Browser launch failed: {exc}"})

    emit_event({"type": "log", "level": "info", "message": f"Done. Applied: {summary.applied} | Needs review: {summary.needs_review} | Skipped: {summary.skipped} | Failed: {summary.failed}"})
    emit_event({"type": "summary", "applied": summary.applied, "needs_review": summary.needs_review, "skipped": summary.skipped, "failed": summary.failed, "total": summary.total})
    # Note: 'done' event is sent by the SSE generator in main.py via the None sentinel.
    # Do NOT emit it here to avoid duplicates.
    return summary


# ── Job-URL collection ────────────────────────────────────────────────────────

async def _collect_job_urls(
    req:     AutoApplyRequest,
    context,
    emit:    Callable,
    summary: SessionSummary,
) -> List[str]:
    """Return job-view URLs from the most specific source available."""
    search_urls: List[str] = []

    # Single job URL provided directly
    if req.job_url:
        summary.search_url = req.job_url
        emit({"type": "log", "level": "info", "message": f"Using supplied job URL: {req.job_url}"})
        # Inspect the single job and only return it if it leads to an external apply portal
        try:
            check_page = await new_stealth_page(context)
            try:
                res = await extract_job_details(check_page, req.job_url)
            finally:
                try:
                    await check_page.close()
                except Exception:
                    pass
            if res.apply_type == "external" and res.apply_url:
                return [req.job_url]
            emit({"type": "log", "level": "info", "message": "Supplied job is Easy Apply or has no external portal; skipping."})
            return []
        except Exception:
            emit({"type": "log", "level": "warning", "message": f"Failed to inspect supplied job URL: {req.job_url}. Skipping."})
            return []

    # Navigate to search (supplied URL or built from keywords)
    if req.search_url:
        search_urls = [req.search_url]
    elif req.keywords:
        search_urls = build_search_urls(req.keywords, req.filters)
    else:
        emit({"type": "error", "message": "Provide at least one of: keywords, search_url, or job_url."})
        return []

    summary.search_urls = search_urls
    summary.search_url = search_urls[0] if len(search_urls) == 1 else None
    if search_urls:
        emit({"type": "log", "level": "info", "message": f"Navigating to search: {search_urls[0]}"})

    urls: List[str] = []
    seen: set[str] = set()

    for search_url in search_urls:
        if len(urls) >= req.max_jobs:
            break
        page = await new_stealth_page(context)
        try:
            try:
                await goto_with_retries(page, search_url, timeout=30_000)
            except PWTimeout:
                logger.warning(f"Search page timed out while loading: {search_url}")
                # The page may still contain enough markup for selector-based discovery.
            await human_delay(2.5, 4.0)
            remaining = req.max_jobs - len(urls)
            found = await discover_job_cards(page, remaining)
            for job_url in found:
                if job_url not in seen:
                    seen.add(job_url)
                    urls.append(job_url)
                    if len(urls) >= req.max_jobs:
                        break
        finally:
            await page.close()

    # Post-filter: only keep jobs that have an external apply URL (skip Easy Apply)
    filtered: List[str] = []
    for job_url in urls:
        if len(filtered) >= req.max_jobs:
            break
        check_page = await new_stealth_page(context)
        try:
            try:
                res = await extract_job_details(check_page, job_url)
            except Exception as exc:
                emit({"type": "log", "level": "warning", "message": f"Failed to inspect job {job_url}: {exc}"})
                continue
            if res.apply_type == "external" and res.apply_url:
                filtered.append(job_url)
            elif res.apply_type == "easy_apply":
                emit({"type": "log", "level": "info", "message": f"Skipping Easy Apply job (not handled): {job_url}"})
            elif res.error in ("captcha_required", "login_required", "registration_required"):
                emit({"type": "log", "level": "warning", "message": f"Skipping — LinkedIn wall ({res.error}) while inspecting: {job_url}"})
            elif res.apply_type == "external" and not res.apply_url:
                emit({"type": "log", "level": "warning", "message": f"External job but could not resolve portal URL: {job_url}"})
            else:
                emit({"type": "log", "level": "info", "message": f"Skipping (no external apply / {res.apply_type or 'unknown'}): {job_url}"})
        finally:
            try:
                await check_page.close()
            except Exception:
                pass

    return filtered


# ── Single-job processing ─────────────────────────────────────────────────────

async def _process_one_job(
    *,
    idx:         int,
    total:       int,
    job_url:     str,
    req:         AutoApplyRequest,
    context,
    detail_page,
    diag:        DiagnosticsCapture,
    emit:        Callable,
    apply_external: bool,
    session_id:  str,
) -> JobResult:
    """Run the full lifecycle for one job URL."""
    from .portal import check_pause_state
    await check_pause_state(session_id)

    _emit(emit, "job_start", index=idx, total=total,
          info={"url": job_url})

    # ── Extract details ───────────────────────────────────────────────────────
    result: JobResult
    try:
        result = await extract_job_details(detail_page, job_url)
    except Exception as exc:
        logger.error(f"Detail extraction failed for {job_url}: {exc}")
        result        = JobResult(job_id=job_url.rsplit("/", 1)[-1])
        result.status = JobStatus.FAILED
        result.error  = str(exc)
        result.diagnostics_dir = await diag.capture(detail_page, "detail_fail")
        return result

    _emit(emit, "log", level="info",
          message=f"[{idx}/{total}] {result.company} – {result.title} "
                  f"({result.status})")

    # ── Skip non-external jobs ────────────────────────────────────────────────
    if result.status == JobStatus.SKIPPED or not result.apply_url:
        return result

    if req.filters.easy_apply_only and result.apply_type != "easy_apply":
        result.status = JobStatus.SKIPPED
        result.error = "Filtered out: not Easy Apply"
        return result

    if not apply_external:
        return result

    # ── Autofill external portal ──────────────────────────────────────────────
    portal_page = None
    try:
        # Some LinkedIn external apply links redirect to talentcommunity URLs
        # which occasionally return LinkedIn error pages (error404). Detect
        # that case and attempt a fallback: open the original job page and
        # look for an external "apply" href (company careers/ATS) to use.
        apply_url_to_use = result.apply_url
        portal_page = None

        # First, attempt to click the LinkedIn Apply button and capture a
        # newly opened portal tab (preferred). This avoids reopening the
        # URL and preserves the browser context of the popup.
        try:
            try:
                popup_page, popup_url = await open_external_apply_page(context, job_url, detail_page)
            except Exception:
                popup_page, popup_url = None, None
            if popup_page:
                portal_page = popup_page
                if popup_url:
                    apply_url_to_use = popup_url
                _emit(emit, "log", level="info", message=f"Captured portal page via Apply click: {apply_url_to_use}")
        except Exception:
            portal_page = None

        # If we didn't capture a popup, fall back to loading the apply URL
        # directly and use heuristic fallbacks to find an external link.
        if not portal_page:
            apply_url_to_use = result.apply_url
            try:
                check_page = await new_stealth_page(context)
                await goto_with_retries(check_page, result.apply_url, timeout=30_000)
                # quick heuristic: LinkedIn error pages include id="error404" on body
                body_id = await check_page.eval_on_selector('body', 'e => e.id')
                if body_id and 'error404' in body_id:
                    # look for external anchors on the original job URL
                    await check_page.close()
                    alt_page = await new_stealth_page(context)
                    try:
                        await goto_with_retries(alt_page, job_url, timeout=30_000)
                    except Exception:
                        pass
                    anchors = await alt_page.query_selector_all('a')
                    found_external = None
                    for a in anchors:
                        href = await a.get_attribute('href')
                        if href and href.startswith('http') and 'linkedin.com' not in href:
                            found_external = href
                            break
                    try:
                        await alt_page.close()
                    except Exception:
                        pass
                    if found_external:
                        apply_url_to_use = found_external
                        _emit(
                            emit,
                            "log",
                            level="info",
                            message=f"Resolved portal fallback URL: {apply_url_to_use}",
                        )
                else:
                    await check_page.close()
            except Exception:
                # if anything goes wrong, fall back to the original apply_url
                try:
                    await check_page.close()
                except Exception:
                    pass

        _emit(
            emit,
            "log",
            level="info",
            message=f"Opening portal apply URL: {apply_url_to_use}",
        )

        # If we already captured a portal Page, pass it directly to autofill.
        if portal_page:
            portal_page, filled, portal_state, outcome = await autofill_portal_form(
                context,
                portal_page,
                req.candidate,
                login_credentials=req.login_credentials,
                emit=emit,
                session_id=session_id,
            )
        else:
            portal_page, filled, portal_state, outcome = await autofill_portal_form(
                context,
                apply_url_to_use,
                req.candidate,
                login_credentials=req.login_credentials,
                emit=emit,
                session_id=session_id,
            )
        result.filled_fields = filled
        result.portal_state = portal_state
        if portal_state == "registration_required":
            result.needs_registration = True
        elif portal_state in {"login_required", "captcha_required"}:
            result.needs_login = True

        # ── Honest, confirmation-based status ─────────────────────────────
        # APPLIED is reserved for a real submission confirmation. Merely
        # filling fields is NOT "applied".
        if outcome == "confirmed":
            result.status = JobStatus.APPLIED
            _emit(emit, "log", level="success",
                  message=f"  ✓ Application submitted & confirmed ({filled} field(s) filled).")
        elif outcome == "manual":
            result.status = JobStatus.MANUAL_REVIEW
            result.error = f"Autofilled {filled} field(s) but form needs manual completion/CAPTCHA — left open, NOT submitted."
            _emit(emit, "log", level="warning",
                  message=f"  ⚠ Form left open for manual completion ({filled} field(s) filled) — not submitted.")
            result.diagnostics_dir = await diag.capture(portal_page, f"manual_needed_{result.job_id}")
        elif outcome == "submitted_unconfirmed":
            result.status = JobStatus.MANUAL_REVIEW
            result.error = "Submitted but confirmation not detected — please verify."
            _emit(emit, "log", level="warning",
                  message=f"  ⚠ Submit clicked but no confirmation detected ({filled} field(s)) – flagged for review.")
            result.diagnostics_dir = await diag.capture(
                portal_page, f"unconfirmed_{result.job_id}"
            )
        elif (portal_state == "registration_required") or (outcome == "blocked" and result.needs_registration):
            result.status = JobStatus.SKIPPED
            result.error = "Portal registration required"
            _emit(emit, "log", level="warning", message="  ⚠ Registration detected on portal – manual step required.")
        elif portal_state == "login_required":
            result.status = JobStatus.SKIPPED
            result.error = "Portal login required"
            _emit(emit, "log", level="warning", message="  ⚠ Portal login required – manual step required.")
        elif portal_state == "captcha_required" or outcome == "blocked":
            result.status = JobStatus.SKIPPED
            result.error = "Portal CAPTCHA / verification / login required"
            _emit(emit, "log", level="warning", message="  ⚠ CAPTCHA / verification required – manual step required.")
        elif filled > 0:
            # Fields were filled but we never reached a submit/confirmation.
            result.status = JobStatus.MANUAL_REVIEW
            result.error = f"Filled {filled} field(s) but could not submit — please finish manually."
            _emit(emit, "log", level="warning",
                  message=f"  ⚠ Filled {filled} field(s) but no submission detected – flagged for review.")
            result.diagnostics_dir = await diag.capture(
                portal_page, f"filled_no_submit_{result.job_id}"
            )
        else:
            result.status = JobStatus.FAILED
            result.error  = "No form fields could be filled."
            _emit(emit, "log", level="warning",
                  message="  ⚠ No fields filled – check diagnostics.")
            result.diagnostics_dir = await diag.capture(
                portal_page, f"no_fill_{result.job_id}"
            )

    except DomainSkippedError as exc:
        logger.warning(f"Domain skipped for {result.apply_url}: {exc}")
        result.status = JobStatus.SKIPPED
        result.error = str(exc)
        _emit(emit, "log", level="warning", message=f"  ⚠ {exc}")
    except Exception as exc:
        err_msg = str(exc)
        if "MANUAL_REVIEW" in err_msg:
            logger.warning(f"Manual review required for {result.apply_url}: {err_msg}")
            result.status = JobStatus.MANUAL_REVIEW
            result.error = err_msg
            if portal_page:
                result.diagnostics_dir = await diag.capture(
                    portal_page, f"manual_review_{result.job_id}"
                )
            _emit(emit, "log", level="warning", message=f"  ⚠ {err_msg}")
        else:
            logger.error(f"Portal autofill error for {result.apply_url}: {exc}")
            
            # PAUSE CHECKPOINT ON UNEXPECTED ERROR
            from .portal import pause_checkpoint
            await pause_checkpoint(session_id, emit, f"Unexpected error: {err_msg} — please check the browser and fix manually, then resume to continue.")
            
            is_submitted = False
            try:
                success_indicators = ["confirmation", "thank-you", "thank_you", "success", "submitted", "complete", "apply-complete"]
                success_phrases = ["application submitted", "thank you for applying", "we have received your application", "application complete", "you have successfully applied", "your application has been received"]
                
                url = portal_page.url.lower() if portal_page else ""
                body = await portal_page.inner_text('body') if portal_page else ""
                body_lower = body.lower()
                
                if any(ind in url for ind in success_indicators) or any(phrase in body_lower for phrase in success_phrases):
                    is_submitted = True
            except Exception:
                pass

            if is_submitted:
                _emit(emit, "log", level="info", message="Manual submission detected after pause. Skipping retry.")
                result.status = JobStatus.APPLIED
                result.filled_fields = max(result.filled_fields, 1)
            else:
                _emit(emit, "log", level="info", message="Retrying step once after manual intervention...")
                try:
                    portal_page_retry, filled, portal_state, outcome = await autofill_portal_form(
                        context,
                        portal_page if portal_page else apply_url_to_use,
                        req.candidate,
                        login_credentials=req.login_credentials,
                        emit=emit,
                        session_id=session_id
                    )
                    if portal_page_retry:
                        portal_page = portal_page_retry
                    result.filled_fields = filled
                    result.portal_state = portal_state
                    if outcome == "confirmed":
                        result.status = JobStatus.APPLIED
                        _emit(emit, "log", level="success", message=f"  ✓ Application submitted & confirmed after retry ({filled} field(s)).")
                    elif outcome == "submitted_unconfirmed" or filled > 0:
                        result.status = JobStatus.MANUAL_REVIEW
                        result.error = "Submitted/filled after retry but not confirmed — please verify."
                        _emit(emit, "log", level="warning", message=f"  ⚠ Retry filled {filled} field(s) but no confirmation – flagged for review.")
                    else:
                        result.status = JobStatus.FAILED
                        result.error = "No form fields could be filled on retry."
                except Exception as retry_exc:
                    logger.error(f"Portal autofill error after retry for {result.apply_url}: {retry_exc}")
                    result.status = JobStatus.FAILED
                    result.error  = str(retry_exc)
                    if portal_page:
                        result.diagnostics_dir = await diag.capture(
                            portal_page, f"portal_crash_{result.job_id}"
                        )
    finally:
        if portal_page:
            if result.status == JobStatus.APPLIED:
                import time, os
                os.makedirs("screenshots", exist_ok=True)
                safe_job_id = ''.join(c for c in result.job_id if c.isalnum()) if result.job_id else "unknown"
                screenshot_path = f"screenshots/applied_{safe_job_id}_{int(time.time())}.png"
                try:
                    await portal_page.screenshot(path=screenshot_path, full_page=True)
                    _emit(emit, "log", level="success", message=f"Application screenshot saved: {screenshot_path}")
                except Exception:
                    pass
            # Keep the tab OPEN when the form was left for manual completion, so
            # the user can finish + submit it themselves. All other outcomes close.
            if result.status == JobStatus.MANUAL_REVIEW and "manual completion" in (result.error or ""):
                _emit(emit, "log", level="warning",
                      message="  → Leaving this browser tab OPEN so you can finish and submit the application manually.")
            else:
                try:
                    await portal_page.close()
                except Exception:
                    pass

    return result


# ── Utility ───────────────────────────────────────────────────────────────────

def _emit(emit: Optional[Callable], event_type: str, **kwargs) -> None:
    if emit:
        try:
            emit({"type": event_type, **kwargs})
        except Exception:
            pass
