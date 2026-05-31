# LinkedIn Job Auto-Applier

Automate discovery of LinkedIn jobs, open job details, detect external "Apply"
links, open the company portal, autofill the form with your profile, and upload
your resume – all from a clean web UI.

---

## Features

| Capability                   | Detail                                                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Job discovery**            | Keywords → LinkedIn search, direct search URL, or a single job-view URL                                             |
| **JSON API**                 | Search-only and full workflow endpoints return structured JSON for downstream automation                            |
| **Login modes**              | Manual (visible browser + 2FA support) or automated via `.env` credentials                                          |
| **Session persistence**      | Saves cookies so you don't re-login on every run                                                                    |
| **External portal autofill** | Workday · Greenhouse · Lever · Taleo/Oracle · iCIMS · SAP SuccessFactors · generic semantic fill                    |
| **Portal-state detection**   | Flags login / registration / CAPTCHA walls on external portals                                                      |
| **Layered resilience**       | Adapter → semantic (aria-label/placeholder/name) → label-text heuristic → resume upload                             |
| **Diagnostics**              | On any failure: saves `screenshot.png`, `page.html`, `console.json`, `info.json`, and a session-level `session.har` |
| **Live UI**                  | SSE-streamed real-time log, per-job status cards, progress bar, diagnostics browser                                 |

---

## Project Structure

```
linkedin-job-applier/
├── backend/
│   ├── main.py           # FastAPI app – REST + SSE endpoints
│   ├── orchestrator.py   # Pipeline coordinator
│   ├── linkedin.py       # Login, job search, card iteration, detail extraction
│   ├── portal.py         # External portal adapters + semantic fill + resume upload
│   ├── browser.py        # Playwright launch, stealth, session save/restore
│   ├── diagnostics.py    # HTML / PNG / console log capture
│   ├── models.py         # Pydantic request/response models
│   ├── config.py         # All settings from .env
│   └── requirements.txt
├── frontend/
│   └── index.html        # Single-file UI (no build step)
├── data/
│   ├── sessions/         # Saved LinkedIn browser state (gitignored)
│   ├── diagnostics/      # Per-failure artefacts (gitignored)
│   └── uploads/          # Uploaded resumes (gitignored)
├── .env.example
├── .gitignore
├── setup.sh
└── start.sh
```

---

## Quick Start

### 1 — Clone / download and run setup

```bash
chmod +x setup.sh start.sh
./setup.sh
```

This creates `.venv`, installs all Python deps, installs Playwright Chromium,
and copies `.env.example` → `.env`.

### 2 — Configure (optional)

Edit `.env`:

```env
# Leave blank to log in manually (recommended for first run)
LINKEDIN_EMAIL=you@email.com
LINKEDIN_PASSWORD=yourpassword

# Human-like delays between actions (seconds)
MIN_DELAY=1.8
MAX_DELAY=4.5

# Keep false for 2FA support; set true only after session is saved
HEADLESS=false
```

### 3 — Start the server

```bash
./start.sh
```

### 4 — Open the UI

Open `frontend/index.html` in your browser (double-click or `file://` URL).

> **Tip:** The frontend talks to `http://localhost:8000`. Make sure the server
> is running before clicking **Start Automation**.

---

## Usage Guide

### Login / 2FA

On the first run with `HEADLESS=false` (the default), Playwright opens a
visible Chromium window. If you left credentials blank, the UI shows a
**"Manual action required"** banner — log in to LinkedIn in that window and
the bot continues automatically. The session is saved so subsequent runs skip
login entirely.

### Job inputs (use any one)

| Field          | Example                                      |
| -------------- | -------------------------------------------- |
| **Keywords**   | `Python backend developer intern`            |
| **Search URL** | Full LinkedIn `/jobs/search/?keywords=…` URL |
| **Job URL**    | `https://linkedin.com/jobs/view/1234567890/` |

Optional filters:

```json
{
  "filters": {
    "posted_within_hours": 24,
    "locations": ["Bengaluru, Karnataka, India", "Pune, Maharashtra, India"],
    "easy_apply_only": false,
    "remote_only": false,
    "extra_params": {
      "sortBy": "DD"
    }
  }
}
```

### What happens for each job

1. Opens the job detail page, reads title and company.
2. Detects the Apply button type:
   - **Easy Apply** (LinkedIn modal) → **skipped** (handled separately).
   - **External** link → proceeds.
3. Opens the portal URL in a new tab.
4. Runs the matching portal adapter (if recognised), then semantic fill, then
   resume upload.
5. Records the result (applied / skipped / failed) and updates the UI live.
6. On any failure: saves a full diagnostics bundle to `data/diagnostics/`.

---

## Supported Portal Adapters

| Portal               | Regex match                                                  |
| -------------------- | ------------------------------------------------------------ |
| Workday              | `myworkday` · `workday.com`                                  |
| Greenhouse           | `greenhouse.io` · `boards.greenhouse`                        |
| Lever                | `lever.co` · `jobs.lever`                                    |
| Taleo / Oracle       | `taleo.net` · `oracle.taleo`                                 |
| iCIMS                | `icims.com`                                                  |
| SAP SuccessFactors   | `successfactors` · `sap.com/careers`                         |
| **Any other portal** | Semantic fill (aria-label / placeholder / name / label text) |

---

## Adding a New Portal Adapter

Create a function in `backend/portal.py` and decorate it:

```python
@_register(r"myats\.com|careers\.mycompany")
async def _my_ats(page: Page, candidate: CandidateProfile) -> int:
    filled = 0
    # use page.query_selector / page.fill ...
    return filled
```

The decorator registers the URL pattern; the framework calls your function
automatically when the portal URL matches.

---

## Diagnostics

Browse captured failures in the UI via **🗂 Diagnostics**, or inspect files
directly under `data/diagnostics/<label_YYYYMMDD_HHMMSS>/`:

| File                           | Contents                                  |
| ------------------------------ | ----------------------------------------- |
| `screenshot.png`               | Full-page screenshot at time of failure   |
| `page.html`                    | Raw page HTML                             |
| `console.json`                 | Last 300 browser console / error messages |
| `info.json`                    | URL, page title, timestamp                |
| `data/diagnostics/session.har` | Full HAR trace for the whole session      |

---

## API Reference

| Method | Endpoint                        | Description                                                    |
| ------ | ------------------------------- | -------------------------------------------------------------- |
| `POST` | `/api/linkedin/search`          | Return filtered job listings as JSON, no application step      |
| `POST` | `/api/linkedin/workflow`        | Run search + portal handling and return a JSON session summary |
| `POST` | `/api/upload-resume`            | Upload PDF/DOCX, returns `{ path }`                            |
| `POST` | `/api/linkedin/auto-apply`      | Start run; streams SSE events                                  |
| `GET`  | `/api/diagnostics`              | List diagnostic directories                                    |
| `GET`  | `/api/diagnostics/{dir}`        | List files in a directory                                      |
| `GET`  | `/api/diagnostics/{dir}/{file}` | Get file content / PNG                                         |
| `GET`  | `/api/health`                   | Health check                                                   |

Each job result now includes structured metadata such as `location`, `job_description`, `apply_type`, `apply_url`, `external_url`, `portal_state`, and `needs_registration` / `needs_login` flags when the portal blocks automation.

---

## Future Improvements

- [ ] Easy Apply (LinkedIn modal) full wizard handler
- [ ] LLM-powered answer generation for unknown form questions
- [ ] Vision-based fallback using GPT-4o / Claude Vision for unknown portals
- [ ] Job–resume similarity scoring (filter low-match jobs before applying)
- [ ] Scheduling / cron mode
- [ ] Email / Telegram notification on completion

---

## Disclaimer

This tool is for **personal use only**. Automated access may conflict with
LinkedIn's Terms of Service. Use responsibly: keep delays human-like, limit
daily volumes, and never share credentials. You are solely responsible for
your account's safety.
