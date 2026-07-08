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


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED_NEEDS_LOGIN = "PAUSED_NEEDS_LOGIN"
    PAUSED_NEEDS_CAPTCHA = "PAUSED_NEEDS_CAPTCHA"
    PAUSED_NEEDS_OTP = "PAUSED_NEEDS_OTP"
    PAUSED_NEEDS_EMAIL_VERIFICATION = "PAUSED_NEEDS_EMAIL_VERIFICATION"
    PAUSED_NEEDS_USER_ANSWER = "PAUSED_NEEDS_USER_ANSWER"
    PAUSED_BEFORE_SUBMIT = "PAUSED_BEFORE_SUBMIT"
    WAITING_USER_SUBMIT = "WAITING_USER_SUBMIT"
    RESUMED = "RESUMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    NEEDS_REOPEN = "NEEDS_REOPEN"



# ── Sub-Models ────────────────────────────────────────────────────────────────

class Education(BaseModel):
    degree: str = ""
    field_of_study: str = ""
    university: str = ""
    graduation_year: str = ""
    gpa: str = ""

class WorkExperience(BaseModel):
    company: str = ""
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""
    is_current: bool = False

# ── Candidate ─────────────────────────────────────────────────────────────────

class CandidateProfile(BaseModel):
    name:         str            = "" # Kept for backward compatibility, but prefer first_name/last_name
    first_name:   str            = ""
    last_name:    str            = ""
    email:        str            = ""
    phone:        str            = ""
    phone_country_code: str      = ""
    cover_text:   str            = ""
    resume_path:  Optional[str]  = None   # server-side path set after upload
    resume_mode:  str            = "upload"   # upload | generate
    profile_mode: str            = "ai"       # manual | ai
    ai_tailor_email: bool        = True
    ai_tailor_resume_pdf: bool   = True
    target_role:  str            = ""
    current_job_title: str       = ""
    technical_skills: str        = ""
    projects:     str            = ""
    location:     Optional[str]  = None   # e.g. "Meerut, India"
    
    # Structured Address
    street_address: str          = ""
    city:           str          = ""
    state:          str          = ""
    country:        str          = ""
    zip_code:       str          = ""
    
    linkedin_url: Optional[str]  = None
    portfolio_url: Optional[str] = "https://www.linkedin.com/in/prashant-gupta-923885328/"

    # Structured Education & Experience
    education: List[Education] = Field(default_factory=list)
    work_experience: List[WorkExperience] = Field(default_factory=list)

    # Extended Profile
    us_citizen: str = ""
    visa_sponsorship_required: str = ""
    security_clearance: str = ""
    clearance_type: str = ""
    clearance_date: str = ""
    preferred_location: str = ""
    willing_to_relocate: str = "Yes"
    work_authorization: str = ""
    gender: str = ""
    ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""
    # Leave blank by default: a hard-coded low figure like "50K" can auto-filter
    # a candidate out. When empty, the resolver leaves salary fields blank / lets
    # the AI omit them rather than committing to a number.
    salary_expectation: str = ""
    notice_period: str = "2 weeks"
    availability_date: str = ""
    referral_source: str = ""
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
    needs_review: int          = 0   # submitted/filled but not confirmed — manual verify
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

# ── Phase 1 Models ────────────────────────────────────────────────────────────

class TaskEvent(BaseModel):
    id: Optional[int] = None
    task_id: str
    event_type: str
    message: str
    timestamp: float

class Task(BaseModel):
    task_id: str
    job_id: Optional[str] = None
    job_title: Optional[str] = None
    company: Optional[str] = None
    job_url: Optional[str] = None
    external_apply_url: Optional[str] = None
    portal_domain: Optional[str] = None
    detected_ats: Optional[str] = None
    status: TaskStatus = TaskStatus.QUEUED
    pause_reason: Optional[str] = None
    current_url: Optional[str] = None
    resume_uploaded: bool = False
    filled_fields_count: int = 0
    pending_fields_count: int = 0
    diagnostics_path: Optional[str] = None
    created_at: float
    updated_at: float

class ExtractedField(BaseModel):
    field_id: str
    selector: str
    tag: str
    input_type: Optional[str] = None
    label: Optional[str] = None
    aria_label: Optional[str] = None
    placeholder: Optional[str] = None
    name: Optional[str] = None
    id: Optional[str] = None
    required: bool = False
    options: List[str] = Field(default_factory=list)
    nearby_text: Optional[str] = None
    visible: bool = True
    value: Optional[str] = None
    confidence: Optional[float] = None

class FillDecision(BaseModel):
    field_id: str
    action: str  # fill/select/check/upload/skip/ask_user
    value: Optional[str] = None
    confidence: float
    source: str  # rule/gemini/user
    reason: Optional[str] = None
    warning: Optional[str] = None

