"""
pause_manager.py – Detects blocking conditions (CAPTCHA, Login, OTP) and triggers pauses with rich diagnostics.
"""
import json
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List
import time

try:
    from .models import TaskStatus, ExtractedField, FillDecision
    from .task_manager import task_manager
    from .config import DIAGNOSTICS_DIR
except ImportError:
    from models import TaskStatus, ExtractedField, FillDecision
    from task_manager import task_manager
    from config import DIAGNOSTICS_DIR

class PauseManager:
    @staticmethod
    async def detect_pause_condition(page) -> Optional[tuple[TaskStatus, str]]:
        """
        Evaluate the page for known blocking conditions.
        Returns a tuple of (TaskStatus, reason) if a pause is needed, else None.
        """
        # CAPTCHA detection
        captcha_selectors = [
            "iframe[src*='recaptcha']", 
            "iframe[src*='hcaptcha']", 
            "iframe[src*='cloudflare']",
            "#px-captcha"
        ]
        for sel in captcha_selectors:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    return TaskStatus.PAUSED_NEEDS_CAPTCHA, "CAPTCHA detected"
            except Exception:
                pass

        # Login wall detection
        login_indicators = [
            "input[type='password']",
            "button:has-text('Sign In')",
            "button:has-text('Log In')",
            "a:has-text('Sign In')"
        ]
        
        # We only consider it a login wall if there's a password field or explicit login button
        # AND it's likely a login page (e.g. not just a tiny 'sign in' link in the header).
        # For simplicity, we just check for password fields right now.
        try:
            pwd_count = await page.locator("input[type='password']").count()
            if pwd_count > 0:
                return TaskStatus.PAUSED_NEEDS_LOGIN, "Login wall detected"
        except Exception:
            pass

        # OTP / Email verification
        otp_text = [
            "enter the code", "verification code", "one-time password", "sent an email"
        ]
        page_text = ""
        try:
            page_text = await page.content()
            page_text = page_text.lower()
        except Exception:
            pass
            
        for text in otp_text:
            if text in page_text:
                return TaskStatus.PAUSED_NEEDS_OTP, "OTP or email verification required"
                
        return None

    @staticmethod
    async def trigger_pause(
        task_id: str, 
        page, 
        status: TaskStatus, 
        reason: str,
        extracted_fields: Optional[List[ExtractedField]] = None,
        fill_decisions: Optional[List[FillDecision]] = None,
        validation_errors: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Trigger a pause, save diagnostics, and update the TaskManager.
        Returns the diagnostics path.
        """
        # 1. Create diagnostics directory for this task
        diag_dir = Path(DIAGNOSTICS_DIR) / task_id / str(int(time.time()))
        diag_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. Save page HTML
        try:
            html = await page.content()
            with open(diag_dir / "page.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass
            
        # 3. Save Screenshot
        try:
            await page.screenshot(path=str(diag_dir / "screenshot.png"), full_page=True)
        except Exception:
            pass
            
        # 4. Save metadata
        info = {
            "task_id": task_id,
            "status": status.value,
            "reason": reason,
            "url": page.url,
            "timestamp": time.time()
        }
        with open(diag_dir / "info.json", "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
            
        # 5. Save fields & decisions if provided
        if extracted_fields:
            with open(diag_dir / "form_fields.json", "w", encoding="utf-8") as f:
                json.dump([f.model_dump() for f in extracted_fields], f, indent=2)
                
        if fill_decisions:
            with open(diag_dir / "fill_decisions.json", "w", encoding="utf-8") as f:
                json.dump([d.model_dump() for d in fill_decisions], f, indent=2)
                
        if validation_errors:
            with open(diag_dir / "validation_errors.json", "w", encoding="utf-8") as f:
                json.dump(validation_errors, f, indent=2)
                
        # 6. Notify TaskManager
        task_manager.pause_task(task_id, status, reason)
        task_manager.update_task_status(task_id, status, reason, diagnostics_path=str(diag_dir))
        
        return str(diag_dir)

pause_manager = PauseManager()
