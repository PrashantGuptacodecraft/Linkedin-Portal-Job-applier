import os
import time
import pytest
from pathlib import Path

from backend.models import Task, TaskEvent, TaskStatus
from backend.database import init_db, save_task, get_task, get_all_tasks, add_task_event, get_task_events
from backend import config

# Use a temporary database for testing
TEST_DB_DIR = Path(config.DATA_DIR) / "test_data"
TEST_DB_PATH = TEST_DB_DIR / "app.db"

@pytest.fixture(autouse=True)
def setup_teardown():
    # Setup
    import backend.database as db
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    
    # Override the DB_PATH in the database module
    original_db_path = db.DB_PATH
    db.DB_PATH = TEST_DB_PATH
    
    # Also reset thread local connection if exists
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
        
    # Delete test db if exists
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
        
    init_db()
    
    yield
    
    # Teardown
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    db.DB_PATH = original_db_path


def test_task_crud():
    now = time.time()
    task = Task(
        task_id="task_123",
        job_title="Software Engineer",
        company="Tech Corp",
        status=TaskStatus.QUEUED,
        created_at=now,
        updated_at=now
    )
    
    # Create
    save_task(task)
    
    # Read
    fetched = get_task("task_123")
    assert fetched is not None
    assert fetched.task_id == "task_123"
    assert fetched.job_title == "Software Engineer"
    assert fetched.status == TaskStatus.QUEUED
    
    # Update
    fetched.status = TaskStatus.RUNNING
    fetched.updated_at = time.time()
    save_task(fetched)
    
    updated = get_task("task_123")
    assert updated.status == TaskStatus.RUNNING
    
    # Transition to Paused
    updated.status = TaskStatus.PAUSED_NEEDS_LOGIN
    updated.pause_reason = "Login required"
    save_task(updated)
    
    paused = get_task("task_123")
    assert paused.status == TaskStatus.PAUSED_NEEDS_LOGIN
    assert paused.pause_reason == "Login required"

def test_task_events():
    now = time.time()
    task = Task(
        task_id="task_456",
        created_at=now,
        updated_at=now
    )
    save_task(task)
    
    event1 = TaskEvent(
        task_id="task_456",
        event_type="log",
        message="Started task",
        timestamp=now
    )
    add_task_event(event1)
    
    event2 = TaskEvent(
        task_id="task_456",
        event_type="status",
        message="Running",
        timestamp=now + 1
    )
    add_task_event(event2)
    
    events = get_task_events("task_456")
    assert len(events) == 2
    assert events[0].event_type == "log"
    assert events[1].event_type == "status"
    assert events[0].id is not None
    assert events[1].id is not None

def test_get_all_tasks():
    now = time.time()
    t1 = Task(task_id="t1", created_at=now, updated_at=now)
    t2 = Task(task_id="t2", created_at=now + 1, updated_at=now + 1)
    
    save_task(t1)
    save_task(t2)
    
    all_tasks = get_all_tasks()
    assert len(all_tasks) == 2
    # Should be ordered by created_at DESC
    assert all_tasks[0].task_id == "t2"
    assert all_tasks[1].task_id == "t1"
