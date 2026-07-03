#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh  –  One-shot project bootstrap
#
# Usage:  chmod +x setup.sh && ./setup.sh
#
# What it does:
#   1. Creates a Python virtual environment (.venv)
#   2. Installs all Python dependencies
#   3. Installs Playwright Chromium browser
#   4. Copies .env.example → .env  (if .env doesn't exist yet)
#   5. Creates required data directories
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()    { echo -e "${CYAN}▸ $*${NC}"; }
success() { echo -e "${GREEN}✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠ $*${NC}"; }

# ── Python version check ──────────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python)
PY_VERSION=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
REQUIRED="3.10"

info "Detected Python $PY_VERSION"
if [[ "$(printf '%s\n' "$REQUIRED" "$PY_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]]; then
  warn "Python $REQUIRED+ required. Please upgrade."
  exit 1
fi

# ── Virtual environment ───────────────────────────────────────────────────────
if [[ ! -d ".venv" ]]; then
  info "Creating virtual environment…"
  $PYTHON -m venv .venv
  success "Virtual environment created."
else
  info "Virtual environment already exists — skipping."
fi

# Activate
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate 2>/dev/null || true

# ── Dependencies ──────────────────────────────────────────────────────────────
info "Installing Python dependencies…"
pip install --quiet --upgrade pip
pip install --quiet -r backend/requirements.txt
success "Python dependencies installed."

# ── Playwright browsers ───────────────────────────────────────────────────────
info "Installing Playwright Chromium…"
python -m playwright install chromium
python -m playwright install-deps chromium 2>/dev/null || true
success "Playwright Chromium ready."

# ── Patchright browser (undetected fork; best-effort) ─────────────────────────
info "Installing Patchright Chromium (undetected)…"
python -m patchright install chromium 2>/dev/null || warn "Patchright browser install skipped (uses system Chrome via channel='chrome')."

# ── Environment file ──────────────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
  cp .env.example .env
  success ".env created from template — edit it with your credentials."
else
  info ".env already exists — skipping."
fi

# ── Data directories ──────────────────────────────────────────────────────────
mkdir -p data/{sessions,diagnostics,uploads}
success "Data directories ready."

echo ""
success "Setup complete!"
echo ""
echo "  Next steps:"
echo "  1.  Edit .env  and fill in LINKEDIN_EMAIL / LINKEDIN_PASSWORD (optional)"
echo "  2.  Run:  ./start.sh  (or: cd backend && python main.py)"
echo "  3.  Open: http://localhost:8000/frontend/index.html"
echo ""
