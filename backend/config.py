"""
config.py – All runtime settings loaded from .env (or environment).
Nothing is hardcoded; sensitive values live only in the .env file.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR: Path = Path(__file__).parent.parent
DATA_DIR: Path = BASE_DIR / "data"

SESSION_PATH:    Path = DATA_DIR / "sessions"  / "linkedin_state.json"
DIAGNOSTICS_DIR: Path = DATA_DIR / "diagnostics"
UPLOADS_DIR:     Path = DATA_DIR / "uploads"

# Auto-create all required directories on import
for _d in (SESSION_PATH.parent, DIAGNOSTICS_DIR, UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── LinkedIn credentials (optional – can log in manually) ────────────────────

LINKEDIN_EMAIL:    str = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD: str = os.getenv("LINKEDIN_PASSWORD", "")

# ── Gemini / LLM (optional) ──────────────────────────────────────────────────

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# ── Timing / safety ───────────────────────────────────────────────────────────

# Randomised delay between actions (seconds).  Keep ≥1.5 to stay human-like.
MIN_DELAY: float = float(os.getenv("MIN_DELAY", "1.8"))
MAX_DELAY: float = float(os.getenv("MAX_DELAY", "4.5"))

# Default headless mode.  Overridden per-request.
HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"

# ── Browser ───────────────────────────────────────────────────────────────────

def _detect_timezone() -> str:
    """Try to auto-detect the local IANA timezone, fallback to Asia/Kolkata."""
    try:
        import datetime
        tz = datetime.datetime.now().astimezone().tzinfo
        name = str(tz)
        # Python may return abbreviations like 'IST'; we need IANA names
        if "/" in name:
            return name
    except Exception:
        pass
    return "Asia/Kolkata"

BROWSER_TIMEZONE: str = os.getenv("BROWSER_TIMEZONE", _detect_timezone())

USER_AGENT: str = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)
