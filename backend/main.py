"""
main.py – FastAPI application.

Endpoints
---------
POST /api/upload-resume        – Save a resume file, return server path.
POST /api/linkedin/search      – Return filtered LinkedIn job listings as JSON.
POST /api/linkedin/workflow    – Run search + external portal handling and return JSON.
POST /api/linkedin/auto-apply  – Start a run; streams SSE events.
GET  /api/diagnostics          – List captured diagnostic directories.
GET  /api/diagnostics/{name}   – List files inside a diagnostic directory.
GET  /api/health               – Simple health-check.

SSE event stream format
-----------------------
Each line is:  data: <JSON>\n\n
Keepalive:     : keepalive\n\n   (every 15 s while run is in progress)

The frontend connects with EventSource / fetch-streaming and renders events
in real time.

WINDOWS NOTE
------------
Playwright requires asyncio.create_subprocess_exec which only works on
ProactorEventLoop on Windows.  Uvicorn uses SelectorEventLoop by default.
We solve this by running the Playwright pipeline in a dedicated background
thread with its own ProactorEventLoop and bridging events back to the main
loop via a thread-safe queue.
"""

from __future__ import annotations

import asyncio
import sys
import warnings
import json
import os
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

import aiofiles

try:
    from .config import DIAGNOSTICS_DIR, UPLOADS_DIR
    from .models import AutoApplyRequest, CandidateProfile, SessionSummary
    from .orchestrator import run_pipeline
    from .resume_generator import generate_resume
except ImportError:
    from config import DIAGNOSTICS_DIR, UPLOADS_DIR
    from models import AutoApplyRequest, CandidateProfile, SessionSummary
    from orchestrator import run_pipeline
    from resume_generator import generate_resume

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="LinkedIn Job Auto-Applier",
    version="1.0.0",
    description="Automates LinkedIn job discovery and external portal autofill.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Resume upload ─────────────────────────────────────────────────────────────

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _preview_url_for_upload(path: str) -> str:
    return f"/uploads/{Path(path).name}"


@app.post("/api/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    """
    Accept a PDF / DOCX resume and store it under data/uploads/.
    Returns the server-side path that should be set as
    AutoApplyRequest.candidate.resume_path.
    """
    allowed = {".pdf", ".doc", ".docx"}
    ext     = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type {ext!r}. Allowed: {allowed}",
        )

    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest      = Path(UPLOADS_DIR) / safe_name

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(content)} bytes). Max: {_MAX_UPLOAD_BYTES} bytes (10 MB).",
        )

    async with aiofiles.open(dest, "wb") as fh:
        await fh.write(content)

    logger.info(f"Resume saved → {dest}")
    return {"path": str(dest), "filename": file.filename, "preview_url": _preview_url_for_upload(str(dest))}


# ── Playwright thread helper ─────────────────────────────────────────────────
#
# On Windows, Playwright needs a ProactorEventLoop to spawn Chromium.
# Uvicorn's main loop may be a SelectorEventLoop which doesn't support
# subprocess creation.  We run the pipeline in a dedicated thread with
# its own ProactorEventLoop and push SSE events back through a
# thread-safe queue.

def _run_pipeline_in_thread(
    req: AutoApplyRequest,
    queue: asyncio.Queue,
    main_loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event = None,
    session_id: str = None,
    *,
    apply_external: bool = True,
):
    """
    Target for threading.Thread.  Creates a fresh ProactorEventLoop (on
    Windows) or a standard loop (elsewhere) and runs the pipeline to
    completion, pushing events to *queue* on *main_loop*.
    """
    def emit(event: Dict[str, Any]) -> None:
        """Thread-safe enqueue: schedule put_nowait on the main event loop."""
        main_loop.call_soon_threadsafe(queue.put_nowait, event)

    async def _inner():
        try:
            logger.info("Pipeline thread: starting pipeline…")
            await run_pipeline(req, emit=emit, apply_external=apply_external, stop_event=stop_event, session_id=session_id)
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error(f"Pipeline thread uncaught: {exc}\n{tb}")
            emit({"type": "error", "message": str(exc)})
        finally:
            # Signal completion via the main loop
            main_loop.call_soon_threadsafe(queue.put_nowait, None)

    # Create a new event loop for this thread
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_inner())
    finally:
        try:
            loop.close()
        except Exception:
            pass


# ── Auto-apply  (SSE streaming) ───────────────────────────────────────────────

@app.post("/api/linkedin/search", response_model=SessionSummary)
async def search_jobs(req: AutoApplyRequest):
    """Return filtered LinkedIn job listings as structured JSON without applying."""
    # For non-streaming endpoints, run in a thread too
    return await _run_pipeline_async(req, apply_external=False)


@app.post("/api/linkedin/workflow", response_model=SessionSummary)
async def workflow(req: AutoApplyRequest):
    """Run search + external portal handling and return a JSON summary."""
    return await _run_pipeline_async(req, apply_external=True)


async def _run_pipeline_async(
    req: AutoApplyRequest, *, apply_external: bool
) -> SessionSummary:
    """Run the pipeline in a background thread and await its result."""
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[SessionSummary] = loop.create_future()

    def _thread_target():
        async def _inner():
            try:
                summary = await run_pipeline(req, emit=None, apply_external=apply_external)
                loop.call_soon_threadsafe(result_future.set_result, summary)
            except Exception as exc:
                loop.call_soon_threadsafe(result_future.set_exception, exc)

        if sys.platform == "win32":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                tloop = asyncio.ProactorEventLoop()
        else:
            tloop = asyncio.new_event_loop()
        asyncio.set_event_loop(tloop)
        try:
            tloop.run_until_complete(_inner())
        finally:
            try:
                tloop.close()
            except Exception:
                pass

    thread = threading.Thread(target=_thread_target, daemon=True)
    thread.start()
    return await result_future


async def _generate_resume_async(candidate: CandidateProfile) -> str:
    """Generate a resume PDF on a background thread and return the output path."""
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[str] = loop.create_future()
    output_name = f"generated_{uuid.uuid4().hex}.pdf"
    output_path = Path(UPLOADS_DIR) / output_name

    def _thread_target():
        async def _inner():
            try:
                # generate_resume is now async
                generated_path = await generate_resume(candidate, str(output_path))
                loop.call_soon_threadsafe(result_future.set_result, generated_path)
            except Exception as exc:
                loop.call_soon_threadsafe(result_future.set_exception, exc)

        if sys.platform == "win32":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                tloop = asyncio.ProactorEventLoop()
        else:
            tloop = asyncio.new_event_loop()
        asyncio.set_event_loop(tloop)
        try:
            tloop.run_until_complete(_inner())
        finally:
            try:
                tloop.close()
            except Exception:
                pass

    thread = threading.Thread(target=_thread_target, daemon=True)
    thread.start()
    return await result_future


@app.post("/api/linkedin/auto-apply")
async def auto_apply(req: AutoApplyRequest):
    """
    Start one automation run.  Returns a Server-Sent Events stream so the
    frontend can show live progress without polling.

    The pipeline runs in a background thread with its own event loop
    (ProactorEventLoop on Windows) so Playwright can spawn Chromium.
    Events are forwarded to the SSE generator via a thread-safe queue.
    """
    main_loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()
    stop_event = threading.Event()
    session_id = str(uuid.uuid4())[:8]
    _active_stop_events[session_id] = stop_event

    # Launch pipeline in a background thread
    thread = threading.Thread(
        target=_run_pipeline_in_thread,
        args=(req, queue, main_loop, stop_event, session_id),
        kwargs={"apply_external": True},
        daemon=True,
    )
    thread.start()

    return StreamingResponse(
        _sse_generator(queue, stop_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control":            "no-cache",
            "X-Accel-Buffering":        "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/api/generate-resume")
async def generate_resume_endpoint(req: AutoApplyRequest):
    """Generate a role-aware PDF resume and return its server path."""
    if not req.candidate.name and not req.candidate.target_role:
        raise HTTPException(status_code=400, detail="Candidate profile and target role are required.")

    # Manual mode keeps the currently attached resume unless the user
    # explicitly asks for AI resume tailoring.
    if req.candidate.profile_mode.lower() == "manual" and not req.candidate.ai_tailor_resume_pdf:
        if req.candidate.resume_path:
            return {
                "path": req.candidate.resume_path,
                "filename": Path(req.candidate.resume_path).name,
                "target_role": req.candidate.target_role,
                "mode": "manual",
                "preview_url": _preview_url_for_upload(req.candidate.resume_path),
            }

    generated_path = await _generate_resume_async(req.candidate)
    return {
        "path": generated_path,
        "filename": Path(generated_path).name,
        "target_role": req.candidate.target_role,
        "mode": "ai",
        "preview_url": _preview_url_for_upload(generated_path),
    }


async def _sse_generator(
    queue: asyncio.Queue[Dict[str, Any] | None],
    stop_event: threading.Event = None,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted strings from the event queue."""
    keepalive_interval = 15   # seconds
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            if event is None:          # sentinel – run is complete
                yield "data: {\"type\": \"done\"}\n\n"
                break

            try:
                yield f"data: {json.dumps(event)}\n\n"
            except Exception as exc:
                logger.debug(f"SSE serialise error: {exc}")
    except asyncio.CancelledError:
        logger.info("SSE client disconnected from stream.")
        raise


# ── Diagnostics endpoints ─────────────────────────────────────────────────────

def _safe_diagnostics_path(*parts: str) -> Path:
    """Resolve a diagnostics sub-path and guard against path traversal."""
    base = Path(DIAGNOSTICS_DIR).resolve()
    target = (base / Path(*parts)).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="Invalid path.")
    return target


@app.get("/api/diagnostics")
async def list_diagnostics():
    """Return a list of all captured diagnostic run directories."""
    base = Path(DIAGNOSTICS_DIR)
    if not base.exists():
        return {"directories": []}
    dirs = sorted(
        [d.name for d in base.iterdir() if d.is_dir()],
        reverse=True,
    )
    return {"directories": dirs}


@app.get("/api/diagnostics/{dir_name}")
async def get_diagnostic_files(dir_name: str):
    """List files inside a specific diagnostic directory."""
    target = _safe_diagnostics_path(dir_name)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Diagnostic directory not found.")
    files = [f.name for f in target.iterdir() if f.is_file()]
    return {"directory": dir_name, "files": sorted(files)}


@app.get("/api/diagnostics/{dir_name}/{filename}")
async def get_diagnostic_file(dir_name: str, filename: str):
    """Return the raw content of a diagnostic file (HTML / JSON / PNG)."""
    target = _safe_diagnostics_path(dir_name, filename)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    if filename.endswith(".png"):
        from fastapi.responses import FileResponse
        return FileResponse(str(target), media_type="image/png")
    async with aiofiles.open(target, "r", encoding="utf-8", errors="replace") as fh:
        content = await fh.read()
    return JSONResponse({"content": content})


# ── Control endpoints ──────────────────────────────────────────────────────────

from pydantic import BaseModel

class SessionControlRequest(BaseModel):
    session_id: str

@app.post("/api/pause")
async def pause_session(req: SessionControlRequest):
    try:
        from .portal import trigger_pause_external
    except ImportError:
        from portal import trigger_pause_external
    
    trigger_pause_external(req.session_id)
    return {"status": "paused"}

# Global dictionary to hold stop events
_active_stop_events = {}

@app.post("/api/stop")
async def stop_session(req: SessionControlRequest):
    if req.session_id in _active_stop_events:
        _active_stop_events[req.session_id].set()
        return {"status": "stopping"}
    return {"status": "not_found"}

@app.post("/api/resume")
async def resume_session(req: SessionControlRequest):
    try:
        from .portal import trigger_resume_external
    except ImportError:
        from portal import trigger_resume_external
        
    trigger_resume_external(req.session_id)
    return {"status": "resuming"}

@app.get("/api/status")
async def get_status(session_id: str):
    try:
        from .portal import get_pause_status
    except ImportError:
        from portal import get_pause_status
        
    status = get_pause_status(session_id)
    return {"status": "ok", "session_id": session_id, **status}

# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": app.version}


# ── Serve built frontend (optional) ───────────────────────────────────────────

_STATIC = Path(__file__).parent.parent / "frontend" / "dist"
_FRONTEND = Path(__file__).parent.parent / "frontend"
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
if _STATIC.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
elif _FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="static")


# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
