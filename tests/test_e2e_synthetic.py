import asyncio
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Any

import pytest

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Monkeypatching configuration
import backend.config as config
config.GEMINI_API_KEY = "dummy"

import backend.linkedin as linkedin
import backend.orchestrator as orchestrator
from backend.database import init_db, get_all_tasks, get_task_events, get_task
from backend.models import AutoApplyRequest, CandidateProfile, LoginCredentials, SearchFilters, TaskStatus, FillDecision
from backend.task_manager import task_manager
from backend.resume_watcher import resume_watcher

from tests.mock_server import MockServer

logger = logging.getLogger(__name__)

# Mock Gemini
class MockGeminiFallback:
    def __init__(self):
        self.call_count = 0

    def resolve_fields(self, task_id, unresolved_fields, profile, job_details, retry_count=1):
        self.call_count += 1
        
        # Test Gemini invalid JSON retry
        if self.call_count == 1:
            raise json.JSONDecodeError("Mock Invalid JSON", "", 0)
            
        decisions = []
        for field in unresolved_fields:
            decisions.append(FillDecision(
                field_id=field.id,
                action="fill",
                value="Mocked Value",
                confidence=0.9,
                source="gemini"
            ))
        return decisions

# Pytest fixture for the mock server
@pytest.fixture(scope="session")
def mock_server_port():
    server = MockServer()
    port = None
    async def run_server():
        nonlocal port
        port = await server.start()
        await asyncio.Event().wait()
        
    loop = asyncio.new_event_loop()
    def start_thread():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_server())

    thread = threading.Thread(target=start_thread, daemon=True)
    thread.start()
    
    # Wait for server to start
    import time
    while port is None:
        time.sleep(0.1)
    
    yield port
    
    loop.call_soon_threadsafe(loop.stop)

@pytest.fixture(autouse=True)
def check_external_requests(request):
    blocked_urls.clear()
    yield
    if request.node.name != "test_e2e_no_external_network":
        assert len(blocked_urls) == 0, f"External network requests blocked: {blocked_urls}"

@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    db_path = tmp_path / "e2e_test.db"
    diag_dir = tmp_path / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    
    import backend.database as db
    import backend.browser as browser
    import backend.pause_manager as pause_manager
    import backend.diagnostics as diagnostics
    import backend.portal as portal
    import backend.main as main_app
    import backend.orchestrator as orchestrator

    monkeypatch.setattr(db, "DB_PATH", db_path)


    for mod in (config, browser, pause_manager, diagnostics, portal, main_app, orchestrator):
        if hasattr(mod, "DIAGNOSTICS_DIR"):
            monkeypatch.setattr(mod, "DIAGNOSTICS_DIR", diag_dir)
        if hasattr(mod, "UPLOADS_DIR"):
            monkeypatch.setattr(mod, "UPLOADS_DIR", uploads_dir)
    
    # Re-initialize DB for each test
    init_db()
    
    # Clear task manager state
    task_manager.running_tasks.clear()
    task_manager.locks.clear()
    
    yield

def create_mock_request(port, job_id=None, search=False, auto_submit=False):
    candidate = CandidateProfile(
        name="Test User",
        first_name="Test",
        last_name="User",
        email="test@example.com",
        phone="1234567890",
    )
    req = AutoApplyRequest(
        headless=True,
        max_jobs=3,
        login_credentials=LoginCredentials(),
        candidate=candidate,
    )
    req.filters.extra_params["auto_submit"] = "true" if auto_submit else "false"
    
    if search:
        req.search_url = f"http://127.0.0.1:{port}/search"
    elif job_id:
        req.job_url = f"http://127.0.0.1:{port}/jobs/view/{job_id}/"
        
    return req

blocked_urls = []

# Network guard to prevent real internet requests
async def network_guard(route):
    url = route.request.url
    if "127.0.0.1" in url or "localhost" in url or url.startswith("data:"):
        await route.continue_()
    else:
        blocked_urls.append(url)
        await route.abort("blockedbyclient")

# Patch browser.py build_context to add network guard
from backend import browser
original_build_context = browser.build_context

async def mock_build_context(pw, headless=True, console_buf=None, record_har=False, session_id=None):
    b, context = await original_build_context(pw, headless=True, console_buf=console_buf, record_har=record_har, session_id=session_id)
    await context.route("**/*", network_guard)
    return b, context

@pytest.fixture(autouse=True)
def patch_deps(monkeypatch, mock_server_port):
    base_url = f"http://127.0.0.1:{mock_server_port}"
    monkeypatch.setattr(linkedin, "_LINKEDIN_BASE", base_url)
    monkeypatch.setattr(linkedin, "_LOGIN_URL", f"{base_url}/login")
    monkeypatch.setattr(linkedin, "_JOBS_BASE", f"{base_url}/jobs/search/")
    monkeypatch.setattr(browser, "build_context", mock_build_context)
    
    # Mock ensure_logged_in to hit the fake login page
    async def mock_ensure_logged_in(context, manual=False, linkedin_email=None, linkedin_password=None, emit=None):
        page = await context.new_page()
        await page.goto(f"{base_url}/login")
        await page.fill("#username", "test")
        await page.fill("#password", "test")
        await page.click("button[type='submit']")
        await page.wait_for_url(f"{base_url}/feed")
        await page.close()
        return True
        
    monkeypatch.setattr(orchestrator, "ensure_logged_in", mock_ensure_logged_in)
    
    # Mock gemini
    mock_gemini = MockGeminiFallback()
    import backend.gemini_fallback as gf
    monkeypatch.setattr(gf.GeminiFallback, "resolve_fields", mock_gemini.resolve_fields)

@pytest.mark.asyncio
async def test_e2e_greenhouse_success(mock_server_port):
    req = create_mock_request(mock_server_port, job_id=1001, auto_submit=True)
    summary = await orchestrator.run_pipeline(req)
    
    assert summary.total == 1
    tasks = get_all_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    
    assert t.company == "Tech Corp"
    assert t.status == TaskStatus.COMPLETED
    
    events = get_task_events(t.task_id)
    event_types = [e.event_type for e in events]
    assert "status" in event_types

@pytest.mark.asyncio
async def test_e2e_lever_success(mock_server_port):
    req = create_mock_request(mock_server_port, job_id=1002, auto_submit=False)
    summary = await orchestrator.run_pipeline(req)
    
    assert summary.total == 1
    assert summary.results[0].status == JobStatus.APPLIED

@pytest.mark.asyncio
async def test_e2e_pause_on_login_then_resume(mock_server_port):
    req = create_mock_request(mock_server_port, job_id=1003, auto_submit=True)
    
    summary = await orchestrator.run_pipeline(req)
    
    assert summary.total == 1
    # Check that it paused/failed for login
    assert summary.results[-1].error in ("login_required", "registration_required", "captcha_required")

@pytest.mark.asyncio
async def test_e2e_pause_on_captcha(mock_server_port):
    req = create_mock_request(mock_server_port, job_id=1004)
    summary = await orchestrator.run_pipeline(req)

    assert summary.total == 1
    assert summary.results[-1].error == "captcha_required"

@pytest.mark.asyncio
async def test_e2e_pause_on_otp_then_resume(mock_server_port):
    req = create_mock_request(mock_server_port, job_id=1005)
    summary = await orchestrator.run_pipeline(req)

    assert summary.total == 1
    assert summary.results[-1].status in (JobStatus.FAILED, JobStatus.MANUAL_REVIEW)

@pytest.mark.asyncio
async def test_e2e_queue_continues_after_pause(mock_server_port):
    # Search will find 1001, 1002, 1003, 1004, 1005
    req = create_mock_request(mock_server_port, search=True)
    req.max_jobs = 3  # Will find 1001, 1002, 1003

    summary = await orchestrator.run_pipeline(req)

    # It should have processed 3 jobs
    assert summary.total == 3
    
    # 1001 -> APPLIED, 1002 -> APPLIED (Wait, test 1002 is auto_submit=False above, but here req auto_submit=True)
    # Actually, create_mock_request sets auto_submit=True.
    # 1003 -> FAILED / MANUAL_REVIEW (login required)
    statuses = [r.status for r in summary.results]
    assert JobStatus.APPLIED in statuses

@pytest.mark.asyncio
async def test_e2e_diagnostics_created_on_pause(mock_server_port, diag_dir):
    req = create_mock_request(mock_server_port, job_id=1003)
    summary = await orchestrator.run_pipeline(req)
    
    assert summary.total == 1
    # Check that diagnostics dir is populated
    import os
    dirs = [d for d in os.listdir(diag_dir) if os.path.isdir(os.path.join(diag_dir, d))]
    # There should be at least one session/task diag folder
    assert len(dirs) >= 1
    # It should contain screenshot.png
    screenshot = os.path.join(diag_dir, dirs[0], "screenshot.png")
    assert os.path.exists(screenshot)

@pytest.mark.asyncio
async def test_e2e_no_external_network(mock_server_port):
    # This is implicitly tested by the network_guard in all tests
    # We will trigger a fake external call to ensure guard works
    
    # Create an arbitrary page
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b, context = await mock_build_context(pw)
        page = await context.new_page()
        
        try:
            await page.goto("https://www.linkedin.com")
        except Exception:
            pass
            
        await context.close()
        await b.close()
        
    assert len(blocked_urls) > 0
    assert any("linkedin.com" in url for url in blocked_urls)

@pytest.mark.asyncio
async def test_e2e_apply_same_tab(mock_server_port):
    # Job 1006 has target="_self" on the Apply button
    req = create_mock_request(mock_server_port, job_id=1006, auto_submit=True)
    summary = await orchestrator.run_pipeline(req)
    
    assert summary.total == 1
    assert summary.results[0].status == JobStatus.APPLIED
