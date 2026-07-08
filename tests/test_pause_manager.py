import pytest
import asyncio
from backend.pause_manager import PauseManager
from backend.models import TaskStatus, ExtractedField, FillDecision
import backend.database as db
from pathlib import Path
from backend import config
import os

TEST_DB_DIR = Path(config.DATA_DIR) / "test_data_pm"
TEST_DB_PATH = TEST_DB_DIR / "app.db"

class MockLocator:
    def __init__(self, count_val):
        self._count = count_val
        
    async def count(self):
        return self._count

class MockPage:
    def __init__(self, html_content="", locators=None):
        self._html_content = html_content
        self._locators = locators or {}
        self.url = "http://test.com"
        
    async def content(self):
        return self._html_content
        
    def locator(self, selector):
        return MockLocator(self._locators.get(selector, 0))
        
    async def screenshot(self, path=None, full_page=False):
        if path:
            with open(path, "wb") as f:
                f.write(b"mock_image")

@pytest.fixture(autouse=True)
def setup_teardown():
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    original_db_path = db.DB_PATH
    db.DB_PATH = TEST_DB_PATH
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
        
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
        
    db.init_db()
    
    # Initialize a task
    from backend.models import Task
    db.save_task(Task(task_id="t1", created_at=0, updated_at=0))
    
    yield
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    db.DB_PATH = original_db_path

def test_detect_captcha():
    page = MockPage(locators={"iframe[src*='recaptcha']": 1})
    result = asyncio.run(PauseManager.detect_pause_condition(page))
    assert result is not None
    status, reason = result
    assert status == TaskStatus.PAUSED_NEEDS_CAPTCHA

def test_detect_login():
    page = MockPage(locators={"input[type='password']": 1})
    result = asyncio.run(PauseManager.detect_pause_condition(page))
    assert result is not None
    status, reason = result
    assert status == TaskStatus.PAUSED_NEEDS_LOGIN

def test_detect_otp():
    page = MockPage(html_content="Please enter the one-time password sent to your email.")
    result = asyncio.run(PauseManager.detect_pause_condition(page))
    assert result is not None
    status, reason = result
    assert status == TaskStatus.PAUSED_NEEDS_OTP

def test_trigger_pause():
    page = MockPage(html_content="<html>test</html>")
    fields = [ExtractedField(field_id="f1", selector="#f1", tag="input")]
    
    diag_path = asyncio.run(PauseManager.trigger_pause(
        task_id="t1",
        page=page,
        status=TaskStatus.PAUSED_NEEDS_USER_ANSWER,
        reason="Need user input",
        extracted_fields=fields
    ))
    
    p = Path(diag_path)
    assert p.exists()
    assert (p / "page.html").exists()
    assert (p / "screenshot.png").exists()
    assert (p / "info.json").exists()
    assert (p / "form_fields.json").exists()
    
    # Check if task manager got updated
    from backend.database import get_task
    task = get_task("t1")
    assert task.status == TaskStatus.PAUSED_NEEDS_USER_ANSWER
    assert task.diagnostics_path == diag_path
