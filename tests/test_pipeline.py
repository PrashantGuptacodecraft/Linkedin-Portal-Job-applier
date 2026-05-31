import asyncio
import json
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from models import AutoApplyRequest, CandidateProfile, LoginCredentials, SearchFilters
import orchestrator

async def stub_login(*args, **kwargs):
    print('STUB_LOGIN_CALLED')
    return True

async def main():
    orchestrator.ensure_logged_in = stub_login

    candidate = CandidateProfile(
        name="Test User",
        email="test@example.com",
        phone="000-000-0000",
    )

    req = AutoApplyRequest(
        job_url="https://example.com",
        headless=True,
        max_jobs=1,
        login_credentials=LoginCredentials(),
        candidate=candidate,
    )

    def emit(e):
        print('EVENT:', json.dumps(e))

    summary = await orchestrator.run_pipeline(req, emit=emit, apply_external=False)
    print('SUMMARY:', summary.model_dump())

if __name__ == '__main__':
    asyncio.run(main())
