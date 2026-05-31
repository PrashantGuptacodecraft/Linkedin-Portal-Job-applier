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
echo "UI → open frontend/index.html in your browser"
echo "API → http://localhost:8000"
echo ""

cd backend
python main.py
