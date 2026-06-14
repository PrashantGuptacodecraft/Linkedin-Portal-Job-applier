import requests
import json

payload = {
    "keywords": "Java Developer",
    "search_url": null,
    "job_url": null,
    "max_jobs": 5,
    "headless": False,
    "login_credentials": {
        "linkedin_email": null,
        "linkedin_password": null,
        "gmail_address": null,
        "gmail_app_password": null,
        "portal_email": null,
        "portal_password": null
    },
    "candidate": {
        "name": "Prashant Gupta",
        "target_role": "Java Developer",
        "email": "test@test.com",
        "phone": "1234567890",
        "cover_text": "Cover",
        "technical_skills": "Java",
        "projects": "Project",
        "resume_mode": "upload",
        "profile_mode": "manual",
        "ai_tailor_email": True,
        "ai_tailor_resume_pdf": True,
        "location": "India",
        "linkedin_url": null,
        "resume_path": null,
        "us_citizen": "",
        "visa_sponsorship_required": "",
        "security_clearance": "",
        "clearance_type": "",
        "clearance_date": "",
        "preferred_location": "",
        "willing_to_relocate": "",
        "work_authorization": "",
        "gender": "",
        "ethnicity": "",
        "veteran_status": "",
        "disability_status": "",
        "salary_expectation": "",
        "notice_period": "",
        "years_of_experience": "",
        "custom_answers_text": ""
    },
    "filters": {
        "posted_within_hours": 24,
        "locations": [],
        "easy_apply_only": False,
        "remote_only": False,
        "extra_params": {
            "search_in": "linkedin_jobs"
        }
    }
}

try:
    r = requests.post("http://localhost:8000/api/linkedin/auto-apply", json=payload)
    print("Status code:", r.status_code)
    print("Response body:", r.text)
except Exception as e:
    print("Error:", e)
