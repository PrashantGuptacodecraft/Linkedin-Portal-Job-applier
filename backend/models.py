"""
models.py – All Pydantic models used across the application.
Single source of truth for data shapes; imported by every other module.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING  = "pending"
    APPLIED  = "applied"
    SKIPPED  = "skipped"
    FAILED   = "failed"
    MANUAL_REVIEW = "manual_review"


# ── Candidate ─────────────────────────────────────────────────────────────────

class CandidateProfile(BaseModel):
    name:         str            = ""
    email:        str            = ""
    phone:        str            = ""
    cover_text:   str            = ""
    resume_path:  Optional[str]  = None   # server-side path set after upload
    resume_mode:  str            = "upload"   # upload | generate
    profile_mode: str            = "ai"       # manual | ai
    ai_tailor_email: bool        = True
    ai_tailor_resume_pdf: bool   = True
    target_role:  str            = ""
    technical_skills: str        = ""
    projects:     str            = ""
    location:     Optional[str]  = None   # e.g. "Meerut, India"
    linkedin_url: Optional[str]  = None

    # Extended Profile
    us_citizen: str = "Yes"
    visa_sponsorship_required: str = "No"
    security_clearance: str = "No"
    clearance_type: str = ""
    clearance_date: str = ""
    preferred_location: str = ""
    willing_to_relocate: str = "No"
    work_authorization: str = "Authorized to work in the US"
    gender: str = ""
    ethnicity: str = ""
    veteran_status: str = "I am not a veteran"
    disability_status: str = "I do not have a disability"
    salary_expectation: str = ""
    notice_period: str = "2 weeks"
    years_of_experience: str = ""
    custom_answers_text: str = ""


class LoginCredentials(BaseModel):
    linkedin_email: Optional[str] = None
    linkedin_password: Optional[str] = None
    gmail_address: Optional[str] = None
    gmail_app_password: Optional[str] = None
    portal_email: Optional[str] = None
    portal_password: Optional[str] = None


class SearchFilters(BaseModel):
    posted_within_hours: int = Field(default=24, ge=1, le=168)
    locations: List[str] = Field(default_factory=list)
    extra_params: Dict[str, str] = Field(default_factory=dict)
    remote_only: bool = False
    easy_apply_only: bool = False


# ── Request / Response ────────────────────────────────────────────────────────

class AutoApplyRequest(BaseModel):
    # Job discovery – at least one of these must be provided
    keywords:   Optional[str] = None          # e.g. "Python backend developer"
    search_url: Optional[str] = None          # Full LinkedIn search URL
    job_url:    Optional[str] = None          # Single linkedin.com/jobs/view/… URL

    candidate:  CandidateProfile = Field(default_factory=CandidateProfile)
    login_credentials: LoginCredentials = Field(default_factory=LoginCredentials)
    filters:    SearchFilters = Field(default_factory=SearchFilters)
    max_jobs:   int  = Field(default=5, ge=1, le=100)
    headless:   bool = False


# ── Per-job result ────────────────────────────────────────────────────────────

class JobResult(BaseModel):
    job_id:          str                 = ""
    title:           str                 = ""
    company:         str                 = ""
    location:        Optional[str]       = None
    job_description: Optional[str]       = None
    apply_type:      Optional[str]       = None
    status:          JobStatus           = JobStatus.PENDING
    apply_url:       Optional[str]       = None
    job_url:         Optional[str]       = None
    external_url:    Optional[str]       = None
    error:           Optional[str]       = None
    diagnostics_dir: Optional[str]       = None
    portal_state:    Optional[str]       = None
    needs_registration: bool             = False
    needs_login:     bool                = False
    filled_fields:   int                 = 0


# ── Session summary (returned at end of run) ──────────────────────────────────

class SessionSummary(BaseModel):
    session_id: str
    total:      int            = 0
    applied:    int            = 0
    skipped:    int            = 0
    failed:     int            = 0
    search_url: Optional[str] = None
    search_urls: List[str] = Field(default_factory=list)
    filters:    SearchFilters = Field(default_factory=SearchFilters)
    results:    List[JobResult] = Field(default_factory=list)
    logs:       List[Dict[str, Any]] = Field(default_factory=list)


# ── SSE event shapes (informal contracts with the frontend) ───────────────────
#
#   { "type": "log",         "level": "info|warning|error", "message": "..." }
#   { "type": "job_start",   "index": N, "total": N, "info": {...} }
#   { "type": "job_result",  "job": JobResult.model_dump() }
#   { "type": "summary",     "applied": N, "skipped": N, "failed": N, "total": N }
#   { "type": "manual_login_required", "message": "..." }
#   { "type": "error",       "message": "..." }
#   { "type": "done" }
#   ": keepalive"  (SSE comment – connection heartbeat)
