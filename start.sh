#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh  –  Activate venv and launch the FastAPI backend
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Activate virtual environment (Unix / Git-Bash on Windows)
if [[ -f ".venv/bin/activate" ]]; then
  source .venv/bin/activate
elif [[ -f ".venv/Scripts/activate" ]]; then
  source .venv/Scripts/activate
else
  echo "Virtual environment not found. Run ./setup.sh first."
  exit 1
fi

echo "Starting LinkedIn Job Auto-Applier backend…"
echo "UI  → http://localhost:8000/"
echo "API → http://localhost:8000/api/health"
echo ""

# Launch from the PROJECT ROOT (not backend/) so the "backend.main:app" package
# import string resolves. Reload is off for a stable long-running server.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info
