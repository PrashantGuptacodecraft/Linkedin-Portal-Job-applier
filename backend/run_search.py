import asyncio
import json
from loguru import logger

from models import AutoApplyRequest

from orchestrator import run_pipeline


def emit_event(ev: dict):
    t = ev.get("type", "log")
    if t == "log":
        print(f"LOG [{ev.get('level','info')}] {ev.get('message')}")
    else:
        print(json.dumps(ev))


async def main():
    # Replace this search URL with your desired LinkedIn search page or set keywords below
    search_url = (
        "https://www.linkedin.com/jobs/search/?keywords=python%20developer&location=United%20States"
    )

    req = AutoApplyRequest(
        search_url=search_url,
        max_jobs=5,
        headless=False,
    )

    logger.info("Starting search-based pipeline (watch the browser window)...")
    summary = await run_pipeline(req, emit=emit_event, apply_external=True)
    print("Summary:", summary.model_dump())


if __name__ == '__main__':
    asyncio.run(main())
