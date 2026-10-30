#!/usr/bin/env python3
"""Regenerate the landing page hero image (fastapi_app/static/img/landing-search.webp).

The landing page shows a screenshot of a real search result. Run this
after any change to the app's look so the two stay in step:

    python3 tools/landing_screenshot.py

It starts the app on a spare port with the clock pinned to a weekday
05:35 (so the result times never change), opens Civic Centre -> Camps
Bay, hides the form and banners so the result cards fill the frame, and
saves a 1440x1214 WebP (a 720x607 view at 2x). Needs Playwright with
Chromium installed.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# pylint: disable=wrong-import-position
import uvicorn
from PIL import Image
from playwright.sync_api import sync_playwright

import fastapi_app.routes.search as search_route
from fastapi_app.main import app

OUT = REPO / "fastapi_app" / "static" / "img" / "landing-search.webp"
PINNED = datetime(2026, 10, 28, 5, 35, tzinfo=ZoneInfo("Africa/Johannesburg"))
QUERY = "/search?from_stop=Civic+Centre&to_stop=Camps+Bay&day_type=weekday"
VIEW = {"width": 720, "height": 607}

# Keep the header, the results heading, the GPS honesty line and the cards.
TIDY_JS = """
  for (const sel of ['.hero', '.card--search', '.alert', '.recent-searches',
                     '.tab-bar', '.wake-banner'])
    document.querySelectorAll(sel).forEach(e => e.style.display = 'none');
  document.querySelectorAll('.trust-strip__line').forEach(e => {
    if (!/not live GPS/i.test(e.textContent)) e.style.display = 'none';
  });
  document.querySelectorAll('.time-relative').forEach(e => e.remove());
  window.scrollTo(0, 0);
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> tuple[uvicorn.Server, threading.Thread]:
    search_route.get_cape_town_time = lambda: PINNED
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                return server, thread
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("app did not start")


def main() -> None:
    """Start the app, take the screenshot, save it as WebP."""
    port = _free_port()
    server, thread = _serve(port)
    png = OUT.with_suffix(".png")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEW, device_scale_factor=2)
            page.goto(f"http://127.0.0.1:{port}{QUERY}", wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            page.evaluate(TIDY_JS)
            page.wait_for_timeout(300)
            page.screenshot(path=str(png))
            browser.close()
        Image.open(png).convert("RGB").save(OUT, "WEBP", quality=82, method=6)
    finally:
        png.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(timeout=10)  # let the app close its database connections
    print(f"Saved {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
