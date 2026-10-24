"""Journey card overlay must not swallow taps on Preview / ⇅ (QA NO-GO #22).

The card body opens /journey via the .journey-card-hit overlay; controls must
resolve to themselves under elementFromPoint at a 390px viewport.
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CSS = REPO / "fastapi_app" / "static" / "css" / "components.css"


def _query() -> str:
    # Preview is only offered for today's day type, so search today.
    from fastapi_app.routes.search import get_current_day_type

    return ("/?from_stop=Civic+Centre&to_stop=Table+View"
            f"&day_type={get_current_day_type()}&operator=both")


def test_card_front_creates_no_stacking_context():
    rule = re.search(r"\.journey-card-front\s*\{([^}]*)\}", CSS.read_text()).group(1)
    z = re.search(r"z-index:\s*([^;]+);", rule)
    assert z is None or z.group(1).strip() == "auto"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "fastapi_app.main:app", "--port", str(port)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=10)


HIT_JS = """() => {
  const at = (el) => { const r = el.getBoundingClientRect();
    return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); };
  const card = [...document.querySelectorAll('.journey-card')].find(c => c.offsetParent
      && c.querySelector('.btn-trip-preview'));
  if (!card) return null;
  card.scrollIntoView({block: 'center'});
  const prev = card.querySelector('.btn-trip-preview');
  const swap = card.querySelector('.btn-reverse');
  const dur = card.querySelector('.journey-duration') || card.querySelector('.journey-viz');
  return { preview: at(prev) === prev, swap: at(swap) === swap,
           body: at(dur) === card.querySelector('.journey-card-hit') };
}"""


def test_controls_above_overlay_browser(server):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binaries missing in CI
            pytest.skip(f"no browser: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(server + _query())
        page.wait_for_timeout(800)
        res = page.evaluate(HIT_JS)
        browser.close()
    if res is None:
        pytest.skip("no previewable journey card in data")
    assert res == {"preview": True, "swap": True, "body": True}
