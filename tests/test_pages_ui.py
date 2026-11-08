"""Browser checks for the static site. Skips when Chromium is not installed."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ART = Path("/opt/cursor/artifacts")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    subprocess.run(
        [sys.executable, "pages/build_site.py", "--out", "dist"],
        cwd=REPO, check=True,
    )
    port = _free_port()
    cmd = [sys.executable, "pages/dev_server.py", "--port", str(port)]
    with subprocess.Popen(
        cmd, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) as proc:
        url = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=1) as resp:
                    ready = resp.status == 200
                    if ready:
                        break
            except OSError:
                time.sleep(0.1)
        if not ready:
            raise RuntimeError("static server did not start")
        yield url
        proc.terminate()


def _shot(page, name):
    if not ART.exists():
        return
    page.screenshot(path=str(ART / name), full_page=True)


def _search_trip(mobile):
    mobile.wait_for_function("() => document.querySelectorAll('.combobox-option').length > 50")
    assert "Africa/Johannesburg" in mobile.content()
    badge = mobile.locator("#ls-badge")
    assert "unavailable" in (badge.get_attribute("aria-label") or "")
    from_box = mobile.locator('[data-combobox="from_stop"] .combobox-input')
    from_box.click()
    from_box.fill("Camps Bay")
    option = '[data-combobox="from_stop"] .combobox-option'
    mobile.locator(option, has_text="Camps Bay").first.click()
    to_box = mobile.locator('[data-combobox="to_stop"] .combobox-input')
    to_box.click()
    to_box.fill("Civic Centre")
    to_option = '[data-combobox="to_stop"] .combobox-option'
    mobile.locator(to_option, has_text="Civic Centre").first.click()
    mobile.locator("label[for=day-weekday]").click()
    mobile.locator("label[for=when-depart]").click()
    mobile.locator("#at").fill("08:00")
    mobile.locator("#search-btn").click()
    mobile.wait_for_selector(".journey-card")
    assert mobile.locator(".journey-card").count() > 0
    assert "08:00" in mobile.inner_text(".results-meta")
    fits = mobile.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
    assert fits, "search results overflow the mobile viewport"
    _shot(mobile, "halte-search-mobile.png")


def _rest_of_mobile(mobile, site):
    mobile.locator(".journey-card-hit").first.click()
    mobile.wait_for_selector(".journey-stop-row")
    assert mobile.locator(".journey-stop-row").count() >= 2
    assert "All stops" in mobile.inner_text("h2")
    _shot(mobile, "halte-journey-mobile.png")
    mobile.goto(site + "/stop/Civic%20Centre?day_type=weekday&operator=myciti")
    mobile.wait_for_selector(".departure-time")
    assert "Civic Centre" in mobile.inner_text("h1")
    _shot(mobile, "halte-stop-mobile.png")
    mobile.goto(
        site + "/search?from_stop=Camps+Bay&to_stop=Hiddingh"
        "&day_type=weekday&operator=both&when=depart&at=08:00"
    )
    mobile.wait_for_selector(".empty-state-title")
    assert "No routes found" in mobile.inner_text(".empty-state-title")
    _shot(mobile, "halte-no-service-mobile.png")
    mobile.goto(
        site + "/search?from_stop=Cape+Town+Station&to_stop=Bellville+Station"
        "&day_type=sunday&operator=metrorail&when=depart&at=08:00"
    )
    mobile.wait_for_selector(".alert-title")
    assert "No Metrorail trains on Sundays" in mobile.inner_text("body")
    _shot(mobile, "halte-metrorail-sunday-mobile.png")


def test_static_flows_desktop_and_mobile(site):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except Exception as exc:  # browser binaries missing in CI
            pytest.skip(f"no browser: {exc}")
        _landing(browser, site)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            geolocation={"latitude": -33.9249, "longitude": 18.4241},
            permissions=["geolocation"],
        )
        mobile = context.new_page()
        seen = []
        mobile.on("request", lambda req: seen.append(req.url))
        mobile.goto(site + "/search?day_type=weekday&operator=both")
        _search_trip(mobile)
        _rest_of_mobile(mobile, site)
        _map(browser, site)
        browser.close()
    assert not any("onrender.com" in url or ":8000" in url for url in seen)
    assert any("/timetable/weekday.json" in url for url in seen)


def _landing(browser, site):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(site + "/")
    page.wait_for_selector("#static-preview-note")
    assert "Your bus" in page.inner_text("h1")
    assert "not affiliated" in page.content()
    _shot(page, "halte-landing-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(200)
    _shot(page, "halte-landing-mobile.png")


def _map(browser, site):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(site + "/map")
    frame = page.frame_locator("#map-frame")
    frame.locator("#style-toggle").wait_for(state="visible", timeout=20000)
    assert frame.locator("#btn-schematic").is_visible()
    _shot(page, "halte-map-desktop.png")
