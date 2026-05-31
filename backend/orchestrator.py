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
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from playwright.async_api import TimeoutError as PWTimeout, async_playwright

from browser  import build_context, human_delay, save_session
from config   import DIAGNOSTICS_DIR
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
) -> SessionSummary:
    """
    Run the full automation pipeline.

    *emit* is a synchronous callback called with SSE event dicts.
    It is safe to call from any async context (the caller queues them).
    """
    session_id = str(uuid.uuid4())[:8]
    summary    = SessionSummary(session_id=session_id)
    console_buf: list = []

    def emit_event(event: Dict[str, Any]) -> None:
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

                emit_event({"type": "log", "level": "info", "message": "LinkedIn login confirmed."})

                # ── 2. Collect job URLs ───────────────────────────────────────
                job_urls = await _collect_job_urls(req, context, emit_event, summary)
                if not job_urls:
                    emit_event({"type": "log", "level": "warning", "message": "No job URLs found. Check keywords or search URL."})
                    return summary

                summary.total = len(job_urls)
                emit_event({"type": "log", "level": "info", "message": f"Found {len(job_urls)} job(s) to process."})

                # ── 3. Process each job ───────────────────────────────────────
                detail_page = await context.new_page()

                for idx, job_url in enumerate(job_urls, start=1):
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
                    )
                    summary.results.append(result)

                    if result.status == JobStatus.APPLIED:
                        summary.applied += 1
                    elif result.status == JobStatus.SKIPPED:
                        summary.skipped += 1
                    else:
                        summary.failed  += 1

                    emit_event({"type": "job_result", "job": result.model_dump(mode="json")})
                    await human_delay(2.0, 5.0)

                await detail_page.close()

            except Exception as exc:
                logger.error(f"Pipeline error: {exc}\n{traceback.format_exc()}")
                emit_event({"type": "error", "message": f"Pipeline crashed: {exc}"})

            finally:
                await save_session(context)
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

    emit_event({"type": "log", "level": "info", "message": f"Done. Applied: {summary.applied} | Skipped: {summary.skipped} | Failed: {summary.failed}"})
    emit_event({"type": "summary", "applied": summary.applied, "skipped": summary.skipped, "failed": summary.failed, "total": summary.total})
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
        return [req.job_url]

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
        page = await context.new_page()
        try:
            try:
                await page.goto(search_url, timeout=30_000, wait_until="domcontentloaded")
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

    return urls


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
) -> JobResult:
    """Run the full lifecycle for one job URL."""

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
        try:
            check_page = await context.new_page()
            await check_page.goto(result.apply_url, timeout=30_000)
            # quick heuristic: LinkedIn error pages include id="error404" on body
            body_id = await check_page.eval_on_selector('body', 'e => e.id')
            if body_id and 'error404' in body_id:
                # look for external anchors on the original job URL
                await check_page.close()
                alt_page = await context.new_page()
                try:
                    await alt_page.goto(job_url, timeout=30_000)
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
            else:
                await check_page.close()
        except Exception:
            # if anything goes wrong, fall back to the original apply_url
            try:
                await check_page.close()
            except Exception:
                pass

        portal_page, filled, portal_state = await autofill_portal_form(
            context,
            apply_url_to_use,
            req.candidate,
        )
        result.filled_fields = filled
        result.portal_state = portal_state
        if portal_state == "registration_required":
            result.needs_registration = True
        elif portal_state in {"login_required", "captcha_required"}:
            result.needs_login = True

        if filled > 0:
            result.status = JobStatus.APPLIED
            _emit(emit, "log", level="info",
                  message=f"  ✓ Autofilled {filled} field(s) on portal.")
        else:
            if portal_state == "registration_required":
                result.status = JobStatus.SKIPPED
                result.error = "Portal registration required"
                _emit(emit, "log", level="warning", message="  ⚠ Registration detected on portal – manual step required.")
            elif portal_state == "login_required":
                result.status = JobStatus.SKIPPED
                result.error = "Portal login required"
                _emit(emit, "log", level="warning", message="  ⚠ Portal login required – manual step required.")
            elif portal_state == "captcha_required":
                result.status = JobStatus.SKIPPED
                result.error = "Portal CAPTCHA / verification required"
                _emit(emit, "log", level="warning", message="  ⚠ CAPTCHA / verification required – manual step required.")
            else:
                result.status = JobStatus.FAILED
                result.error  = "No form fields could be filled."
                _emit(emit, "log", level="warning",
                      message="  ⚠ No fields filled – check diagnostics.")
                result.diagnostics_dir = await diag.capture(
                    portal_page, f"no_fill_{result.job_id}"
                )

    except Exception as exc:
        logger.error(f"Portal autofill error for {result.apply_url}: {exc}")
        result.status = JobStatus.FAILED
        result.error  = str(exc)
        if portal_page:
            result.diagnostics_dir = await diag.capture(
                portal_page, f"portal_crash_{result.job_id}"
            )
    finally:
        if portal_page:
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
