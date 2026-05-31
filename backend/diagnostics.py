"""
diagnostics.py – Captures page-state artefacts (HTML, PNG, console logs, info JSON)
whenever the bot encounters an unrecoverable error on a portal or LinkedIn page.

The HAR file is recorded at the browser-context level and is therefore saved
automatically when the context closes at the end of a run.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger
from playwright.async_api import Page

from config import DIAGNOSTICS_DIR


class DiagnosticsCapture:
    """
    Usage
    -----
    diag = DiagnosticsCapture(shared_console_buffer)
    ...
    path = await diag.capture(page, "oracle_apply_fail")
    result.diagnostics_dir = path
    """

    def __init__(self, console_log_buffer: List[Dict[str, Any]]):
        # The buffer is a reference to the list that the page's "console" listener
        # appends to throughout the session – capturing recent entries at failure time.
        self._buf = console_log_buffer

    # ── Public API ────────────────────────────────────────────────────────────

    async def capture(self, page: Page, label: str) -> str:
        """
        Save all available diagnostics to a timestamped sub-directory.
        Returns the directory path as a string (stored in JobResult.diagnostics_dir).
        """
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe      = "".join(c if c.isalnum() or c == "_" else "_" for c in label)[:40]
        out_dir   = Path(DIAGNOSTICS_DIR) / f"{safe}_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)

        await self._screenshot(page, out_dir)
        await self._html(page, out_dir)
        self._console(out_dir)
        await self._info(page, out_dir, label)

        logger.info(f"Diagnostics → {out_dir}")
        return str(out_dir)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _screenshot(self, page: Page, out: Path) -> None:
        try:
            await page.screenshot(path=str(out / "screenshot.png"), full_page=True)
        except Exception as exc:
            logger.debug(f"screenshot failed: {exc}")

    async def _html(self, page: Page, out: Path) -> None:
        try:
            html = await page.content()
            (out / "page.html").write_text(html, encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug(f"html capture failed: {exc}")

    def _console(self, out: Path) -> None:
        try:
            # Keep only the last 300 entries to avoid huge files
            (out / "console.json").write_text(
                json.dumps(self._buf[-300:], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug(f"console save failed: {exc}")

    async def _info(self, page: Page, out: Path, label: str) -> None:
        try:
            info = {
                "label":     label,
                "url":       page.url,
                "title":     await page.title(),
                "timestamp": datetime.now().isoformat(),
            }
            (out / "info.json").write_text(
                json.dumps(info, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug(f"info save failed: {exc}")
