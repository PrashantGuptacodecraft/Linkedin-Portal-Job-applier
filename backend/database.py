"""
database.py – SQLite persistence layer for tasks and events.
"""
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

try:
    from .models import Task, TaskEvent, TaskStatus
    from .config import DATA_DIR
except ImportError:
    from models import Task, TaskEvent, TaskStatus
    from config import DATA_DIR

DB_PATH = Path(DATA_DIR) / "app.db"
_local = threading.local()

def get_connection() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn"):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn

def init_db():
    """Initialize the SQLite database schema."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Tasks table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            job_id TEXT,
            job_title TEXT,
            company TEXT,
            job_url TEXT,
            external_apply_url TEXT,
            portal_domain TEXT,
            detected_ats TEXT,
            status TEXT NOT NULL,
            pause_reason TEXT,
            current_url TEXT,
            resume_uploaded BOOLEAN NOT NULL DEFAULT 0,
            filled_fields_count INTEGER NOT NULL DEFAULT 0,
            pending_fields_count INTEGER NOT NULL DEFAULT 0,
            diagnostics_path TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')

    # Task events table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp REAL NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks (task_id)
        )
    ''')
    
    # Placeholder for portal sessions
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS portal_sessions (
            domain TEXT PRIMARY KEY,
            session_data TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
    
    conn.commit()

def save_task(task: Task):
    """Insert or update a task."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO tasks (
            task_id, job_id, job_title, company, job_url, external_apply_url,
            portal_domain, detected_ats, status, pause_reason, current_url,
            resume_uploaded, filled_fields_count, pending_fields_count,
            diagnostics_path, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        task.task_id, task.job_id, task.job_title, task.company, task.job_url, task.external_apply_url,
        task.portal_domain, task.detected_ats, task.status.value, task.pause_reason, task.current_url,
        int(task.resume_uploaded), task.filled_fields_count, task.pending_fields_count,
        task.diagnostics_path, task.created_at, task.updated_at
    ))
    conn.commit()

def get_task(task_id: str) -> Optional[Task]:
    """Retrieve a single task by ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM tasks WHERE task_id = ?', (task_id,))
    row = cursor.fetchone()
    if not row:
        return None
    
    # SQLite booleans are stored as integers
    d = dict(row)
    d['resume_uploaded'] = bool(d['resume_uploaded'])
    d['status'] = TaskStatus(d['status'])
    return Task(**d)

def get_all_tasks() -> List[Task]:
    """Retrieve all tasks ordered by newest first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM tasks ORDER BY created_at DESC')
    rows = cursor.fetchall()
    
    tasks = []
    for row in rows:
        d = dict(row)
        d['resume_uploaded'] = bool(d['resume_uploaded'])
        d['status'] = TaskStatus(d['status'])
        tasks.append(Task(**d))
    return tasks

def add_task_event(event: TaskEvent):
    """Add a timeline event for a task."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO task_events (task_id, event_type, message, timestamp)
        VALUES (?, ?, ?, ?)
    ''', (event.task_id, event.event_type, event.message, event.timestamp))
    conn.commit()
    event.id = cursor.lastrowid

def get_task_events(task_id: str) -> List[TaskEvent]:
    """Retrieve all events for a specific task."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM task_events WHERE task_id = ? ORDER BY timestamp ASC', (task_id,))
    rows = cursor.fetchall()
    return [TaskEvent(**dict(row)) for row in rows]
