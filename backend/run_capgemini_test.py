import asyncio
import os
from orchestrator import run_pipeline
from models import AutoApplyRequest, CandidateProfile

JOB_URL = "https://careers.capgemini.com/job/New-York%2C-NY-Guidewire-Developer-NY-10001/1396106333/?feedId=388933&utm_source=LinkedInJobPostings"

async def main():
    candidate = CandidateProfile(
        name="Test User",
        email="testuser@example.com",
        phone="555-0100",
        cover_text="I am very interested in this role.",
        resume_path=os.path.join("data", "uploads", "4db7ee4319fc4552905bdb7f31f214d3.pdf"),
        location="New York, NY",
        linkedin_url="https://www.linkedin.com/in/test-user"
    )

    req = AutoApplyRequest(job_url=JOB_URL, candidate=candidate, headless=False, max_jobs=1)

    def emit(e):
        print(e)

    summary = await run_pipeline(req, emit=emit, apply_external=True)
    print("SUMMARY:", summary)

if __name__ == '__main__':
    asyncio.run(main())
