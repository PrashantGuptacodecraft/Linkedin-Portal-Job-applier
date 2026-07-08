"""
resume_watcher.py – Periodically polls paused Playwright pages to detect when human intervention is complete.
"""
import asyncio
import time
from typing import Dict

try:
    from .models import TaskStatus
    from .task_manager import task_manager
    from .pause_manager import PauseManager
except ImportError:
    from models import TaskStatus
    from task_manager import task_manager
    from pause_manager import PauseManager

class ResumeWatcher:
    def __init__(self):
        self._watching: Dict[str, asyncio.Task] = {}

    def watch(self, task_id: str, page, original_status: TaskStatus):
        """Start polling a paused page."""
        if task_id in self._watching:
            self._watching[task_id].cancel()
        
        try:
            loop = asyncio.get_running_loop()
            self._watching[task_id] = loop.create_task(self._watch_loop(task_id, page, original_status))
        except RuntimeError:
            pass # No loop

    def stop_watching(self, task_id: str):
        """Stop polling a task."""
        if task_id in self._watching:
            self._watching[task_id].cancel()
            del self._watching[task_id]

    async def _watch_loop(self, task_id: str, page, original_status: TaskStatus):
        try:
            while True:
                await asyncio.sleep(4)
                
                # Check if page is closed
                if page.is_closed():
                    task_manager.update_task_status(task_id, TaskStatus.NEEDS_REOPEN, reason="Browser page was closed.")
                    break
                    
                # 1. Check for success page
                try:
                    content = await page.content()
                    lower_content = content.lower()
                    if "application submitted" in lower_content or "thank you for applying" in lower_content:
                        task_manager.complete_task(task_id)
                        break
                except Exception:
                    pass

                # 2. Check if the block condition is resolved
                new_condition = await PauseManager.detect_pause_condition(page)
                if new_condition is None:
                    # We consider the pause resolved if no known block is detected
                    # BUT if it was PAUSED_NEEDS_USER_ANSWER, we might want to wait for them to click 'next'.
                    # For now, we just resume. The orchestrator will try filling again.
                    task_manager.resume_task(task_id)
                    break
                else:
                    new_status, new_reason = new_condition
                    if new_status != original_status:
                        # Status escalated (e.g. solved CAPTCHA, now hit OTP)
                        task_manager.update_task_status(task_id, new_status, reason=new_reason)
                        original_status = new_status
                        
        except asyncio.CancelledError:
            # Watcher was explicitly stopped
            pass
        except Exception as e:
            task_manager.update_task_status(task_id, TaskStatus.NEEDS_REOPEN, reason=f"Watch loop failed: {str(e)}")
        finally:
            if task_id in self._watching:
                del self._watching[task_id]

resume_watcher = ResumeWatcher()
