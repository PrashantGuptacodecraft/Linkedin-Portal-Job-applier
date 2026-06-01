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
from typing import Any, Dict, Optional

# Auto-create all required directories on import
for _d in (SESSION_PATH.parent, DIAGNOSTICS_DIR, UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── LinkedIn credentials (optional – can log in manually) ────────────────────

LINKEDIN_EMAIL:    str = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD: str = os.getenv("LINKEDIN_PASSWORD", "")

# ── Gemini / LLM / AI ──────────────────────────────────────────────────────────

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# ── Portal Settings ───────────────────────────────────────────────────────────

PORTAL_EMAIL: str = os.getenv("PORTAL_EMAIL", "")
PORTAL_PASSWORD: str = os.getenv("PORTAL_PASSWORD", "")

SKIP_DOMAINS = [d.strip() for d in os.getenv("SKIP_DOMAINS", "jooble.org,indeed.com").split(",") if d.strip()]

class DomainSkippedError(Exception):
    """Raised when a URL's domain matches a domain in the SKIP_DOMAINS list."""
    pass

def get_portal_credentials(url: str, request_creds: Optional[Any] = None) -> Dict[str, str]:
    """
    Returns the portal email and deterministic password for the given domain.
    Also logs the generated credential to data/portal_accounts.json.
    """
    import urllib.parse
    import hashlib
    import json
    
    try:
        domain = urllib.parse.urlparse(url).netloc
        if not domain:
            domain = url
    except Exception:
        domain = "unknown_domain"

    # Prefer UI-provided credentials, fallback to .env
    email = ""
    password_seed = ""
    if request_creds:
        email = getattr(request_creds, "portal_email", "") or ""
        password_seed = getattr(request_creds, "portal_password", "") or ""
    
    if not email:
        email = PORTAL_EMAIL
    if not password_seed:
        password_seed = PORTAL_PASSWORD

    if not email or not password_seed:
        return {"email": "", "password": ""}

    # Deterministic password generation
    raw = f"{password_seed}:{domain}"
    pw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    # Ensure requirements: 1 uppercase, 1 special, 1 number
    generated_pw = pw_hash.capitalize() + "!1a"

    # Log to local file
    try:
        accounts_file = DATA_DIR / "portal_accounts.json"
        accounts = {}
        if accounts_file.exists():
            with open(accounts_file, "r") as f:
                try:
                    accounts = json.load(f)
                except Exception:
                    pass
        
        accounts[domain] = {"email": email, "password": generated_pw}
        with open(accounts_file, "w") as f:
            json.dump(accounts, f, indent=2)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to log portal account: {e}")

    return {"email": email, "password": generated_pw}

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
