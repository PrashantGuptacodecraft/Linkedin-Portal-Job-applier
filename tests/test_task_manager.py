import pytest
import time
from backend.task_manager import TaskManager
from backend.models import Task, TaskStatus
import backend.database as db
from pathlib import Path
from backend import config

TEST_DB_DIR = Path(config.DATA_DIR) / "test_data_tm"
TEST_DB_PATH = TEST_DB_DIR / "app.db"

@pytest.fixture(autouse=True)
def setup_teardown():
    # Setup DB
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    original_db_path = db.DB_PATH
    db.DB_PATH = TEST_DB_PATH
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
        
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
        
    db.init_db()
    
    yield
    
    # Teardown
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    db.DB_PATH = original_db_path


def test_task_manager_concurrency():
    tm = TaskManager(max_concurrency=2)
    
    # Track calls to worker_callback
    started_tasks = []
    def mock_worker(task_id):
        started_tasks.append(task_id)
        
    tm.worker_callback = mock_worker
    
    # Add 3 tasks
    now = time.time()
    t1 = Task(task_id="t1", created_at=now, updated_at=now)
    t2 = Task(task_id="t2", created_at=now, updated_at=now)
    t3 = Task(task_id="t3", created_at=now, updated_at=now)
    
    tm.add_task(t1)
    tm.add_task(t2)
    tm.add_task(t3)
    
    # Only 2 should start because max_concurrency=2
    assert len(started_tasks) == 2
    assert "t1" in started_tasks
    assert "t2" in started_tasks
    assert "t3" not in started_tasks
    assert len(tm.running_tasks) == 2
    
    # Complete t1
    tm.complete_task("t1")
    
    # Now t3 should start
    assert len(started_tasks) == 3
    assert "t3" in started_tasks
    assert "t1" not in tm.running_tasks
    assert len(tm.running_tasks) == 2

def test_task_manager_pause_resumes_capacity():
    tm = TaskManager(max_concurrency=1)
    
    started_tasks = []
    def mock_worker(task_id):
        started_tasks.append(task_id)
        
    tm.worker_callback = mock_worker
    
    now = time.time()
    t1 = Task(task_id="t1", created_at=now, updated_at=now)
    t2 = Task(task_id="t2", created_at=now, updated_at=now)
    
    tm.add_task(t1)
    tm.add_task(t2)
    
    assert len(started_tasks) == 1
    assert "t1" in started_tasks
    
    # Pause t1
    tm.pause_task("t1", TaskStatus.PAUSED_NEEDS_LOGIN, "Need login")
    
    # Capacity is released, t2 should start
    assert len(started_tasks) == 2
    assert "t2" in started_tasks
    assert "t1" not in tm.running_tasks
    assert "t2" in tm.running_tasks
