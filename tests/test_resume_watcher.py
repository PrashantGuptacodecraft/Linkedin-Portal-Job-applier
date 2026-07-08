import pytest
import asyncio
from backend.resume_watcher import ResumeWatcher
from backend.pause_manager import PauseManager
from backend.models import Task, TaskStatus
import backend.database as db
from backend.task_manager import task_manager
from pathlib import Path
from backend import config
import time

TEST_DB_DIR = Path(config.DATA_DIR) / "test_data_rw"
TEST_DB_PATH = TEST_DB_DIR / "app.db"

class MockLocator:
    def __init__(self, count_val):
        self._count = count_val
    async def count(self):
        return self._count

class MockPage:
    def __init__(self, html_content="", locators=None, is_closed_val=False):
        self._html_content = html_content
        self._locators = locators or {}
        self._is_closed = is_closed_val
        self.url = "http://test.com"
        
    async def content(self):
        return self._html_content
        
    def locator(self, selector):
        return MockLocator(self._locators.get(selector, 0))
        
    def is_closed(self):
        return self._is_closed


@pytest.fixture(autouse=True)
def setup_teardown(monkeypatch):
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    original_db_path = db.DB_PATH
    db.DB_PATH = TEST_DB_PATH
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
        
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
        
    db.init_db()
    
    # Mock sleep so tests run instantly
    async def mock_sleep(*args, **kwargs):
        pass
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)
    
    yield
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    db.DB_PATH = original_db_path


def test_resume_when_resolved():
    # Insert a task
    db.save_task(Task(task_id="t1", status=TaskStatus.PAUSED_NEEDS_LOGIN, created_at=0, updated_at=0))
    
    rw = ResumeWatcher()
    
    # Page with no blocking conditions (login is resolved)
    page = MockPage()
    
    async def run_test():
        # Start the watch loop. Since we mocked sleep to return instantly, 
        # it will run the first iteration, resolve, update the task manager, and break.
        await rw._watch_loop("t1", page, TaskStatus.PAUSED_NEEDS_LOGIN)
        
    asyncio.run(run_test())
    
    # Task manager should have updated status to QUEUED, which immediately transitions to RUNNING
    task = db.get_task("t1")
    assert task.status == TaskStatus.RUNNING

def test_resume_when_success():
    db.save_task(Task(task_id="t2", status=TaskStatus.PAUSED_NEEDS_LOGIN, created_at=0, updated_at=0))
    rw = ResumeWatcher()
    
    # Page with success text
    page = MockPage(html_content="application submitted successfully")
    
    async def run_test():
        await rw._watch_loop("t2", page, TaskStatus.PAUSED_NEEDS_LOGIN)
        
    asyncio.run(run_test())
    
    task = db.get_task("t2")
    assert task.status == TaskStatus.COMPLETED

def test_resume_when_closed():
    db.save_task(Task(task_id="t3", status=TaskStatus.PAUSED_NEEDS_LOGIN, created_at=0, updated_at=0))
    rw = ResumeWatcher()
    
    # Page is closed
    page = MockPage(is_closed_val=True)
    
    async def run_test():
        await rw._watch_loop("t3", page, TaskStatus.PAUSED_NEEDS_LOGIN)
        
    asyncio.run(run_test())
    
    task = db.get_task("t3")
    assert task.status == TaskStatus.NEEDS_REOPEN
