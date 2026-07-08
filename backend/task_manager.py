"""
task_manager.py – Manages task queues, concurrency, and SSE broadcasting.
"""
import asyncio
import time
import threading
from typing import Dict, List, Optional, Callable, Any

try:
    from .models import Task, TaskStatus, TaskEvent
    from .database import save_task, get_task, get_all_tasks, add_task_event
except ImportError:
    from models import Task, TaskStatus, TaskEvent
    from database import save_task, get_task, get_all_tasks, add_task_event

class TaskManager:
    def __init__(self, max_concurrency: int = 2):
        self.max_concurrency = max_concurrency
        self.running_tasks: set[str] = set()
        self.locks: Dict[str, float] = {}  # task_id -> lock timestamp
        self._lock_timeout = 300.0         # 5 minutes
        self.lock = threading.Lock()
        
        # SSE broadcasting
        self.subscribers: List[asyncio.Queue] = []
        
        # Callback to actually execute a task
        self.worker_callback: Optional[Callable[[str], Any]] = None

    def _emit(self, event: Dict[str, Any]):
        """Emit event to all connected SSE clients."""
        for q in self.subscribers:
            # Using call_soon_threadsafe if we are outside the event loop
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                # No running event loop, this might happen in tests
                pass

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def log_event(self, task_id: str, event_type: str, message: str):
        event = TaskEvent(task_id=task_id, event_type=event_type, message=message, timestamp=time.time())
        add_task_event(event)
        self._emit({"type": event_type, "task_id": task_id, "message": message, "timestamp": event.timestamp})

    def add_task(self, task: Task):
        save_task(task)
        self.log_event(task.task_id, "status", f"Task {task.task_id} queued.")
        self._emit({"type": "task_added", "task": task.model_dump()})
        self._try_start_next()

    def _acquire_lock(self, task_id: str) -> bool:
        now = time.time()
        with self.lock:
            # Check for stale locks
            if task_id in self.locks:
                if now - self.locks[task_id] > self._lock_timeout:
                    self.locks[task_id] = now
                    return True
                return False
            self.locks[task_id] = now
            return True

    def _release_lock(self, task_id: str):
        with self.lock:
            if task_id in self.locks:
                del self.locks[task_id]

    def _try_start_next(self):
        with self.lock:
            if len(self.running_tasks) >= self.max_concurrency:
                return
        
        # Find queued tasks from DB
        tasks = get_all_tasks()
        queued = [t for t in tasks if t.status == TaskStatus.QUEUED]
        
        for task in queued:
            with self.lock:
                if len(self.running_tasks) >= self.max_concurrency:
                    break
            
            if self._acquire_lock(task.task_id):
                with self.lock:
                    self.running_tasks.add(task.task_id)
                
                # Update status
                task.status = TaskStatus.RUNNING
                task.updated_at = time.time()
                save_task(task)
                self.log_event(task.task_id, "status", "Task started running.")
                
                # Notify worker callback to pick this up
                if self.worker_callback:
                    try:
                        self.worker_callback(task.task_id)
                    except Exception as e:
                        self.fail_task(task.task_id, f"Failed to start worker: {str(e)}")

    def update_task_status(self, task_id: str, new_status: TaskStatus, reason: Optional[str] = None, **kwargs):
        task = get_task(task_id)
        if not task:
            return

        task.status = new_status
        task.pause_reason = reason
        task.updated_at = time.time()
        
        for k, v in kwargs.items():
            if hasattr(task, k):
                setattr(task, k, v)
                
        save_task(task)
        
        msg = f"Status updated to {new_status.value}"
        if reason:
            msg += f": {reason}"
        self.log_event(task_id, "status", msg)
        self._emit({"type": "task_updated", "task": task.model_dump()})
        
        # If terminal or paused, release capacity
        is_terminal = new_status in (
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED, 
            TaskStatus.NEEDS_REOPEN, TaskStatus.QUEUED, TaskStatus.RESUMED
        )
        is_paused = new_status.value.startswith("PAUSED_") or new_status == TaskStatus.WAITING_USER_SUBMIT
        
        if is_terminal or is_paused:
            with self.lock:
                if task_id in self.running_tasks:
                    self.running_tasks.remove(task_id)
            self._release_lock(task_id)
            # Try to start another task
            self._try_start_next()

    def complete_task(self, task_id: str):
        self.update_task_status(task_id, TaskStatus.COMPLETED)

    def fail_task(self, task_id: str, reason: str):
        self.update_task_status(task_id, TaskStatus.FAILED, reason=reason)

    def pause_task(self, task_id: str, status: TaskStatus, reason: str):
        self.update_task_status(task_id, status, reason=reason)

    def resume_task(self, task_id: str):
        # Move back to QUEUED so it can be picked up by try_start_next
        self.update_task_status(task_id, TaskStatus.QUEUED, reason=None)
        # We explicitly don't call try_start_next here because update_task_status does it

# Global singleton
task_manager = TaskManager()
