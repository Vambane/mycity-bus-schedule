"""The static build stays inside Cloudflare's limits and keeps secrets off the client."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from pages.build_site import build

REPO = Path(__file__).resolve().parents[1]


def test_build_is_deterministic_and_within_limits(tmp_path):
    first = build(tmp_path / "a")
    second = build(tmp_path / "b")
    assert first["snapshot"] == second["snapshot"]
    assert first["within_cloudflare_limits"] is True
    assert first["files"] == second["files"]
    assert first["bytes"] == second["bytes"]
    left = tmp_path / "a" / "data" / "v" / first["snapshot"] / "timetable" / "weekday.json"
    right = tmp_path / "b" / "data" / "v" / second["snapshot"] / "timetable" / "weekday.json"
    assert left.read_bytes() == right.read_bytes()


def test_client_tree_has_no_api_key_and_degrades_load_shedding(tmp_path):
    out = tmp_path / "site"
    manifest = build(out)
    texts = []
    for path in out.rglob("*"):
        if path.is_file():
            texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    blob = "\n".join(texts)
    assert "ESP_API_KEY=" not in blob
    assert "Bearer " not in blob
    ls_doc = json.loads((out / "data" / "ls.json").read_text(encoding="utf-8"))
    assert ls_doc["endpoint"] is None
    health = json.loads((out / "health.json").read_text(encoding="utf-8"))
    assert health["status"] == "ok"
    assert health["mode"] == "static"
    assert health["db_ready"] is True
    assert "Africa/Johannesburg" in health["scraped_at_display"]
    redirects = (out / "_redirects").read_text(encoding="utf-8")
    assert "/stop/*" in redirects
    assert "/health" in redirects
    headers = (out / "_headers").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in headers
    assert "X-Content-Type-Options: nosniff" in headers
    weekday = out / "data" / "v" / manifest["snapshot"] / "timetable" / "weekday.json"
    assert len(gzip.compress(weekday.read_bytes())) < 200_000
    search = (out / "search" / "index.html").read_text(encoding="utf-8")
    assert 'id="search-form"' in search
    assert "Africa/Johannesburg" in search
    assert "Load shedding unavailable" in search or "ls-badge" in search
    landing = (out / "index.html").read_text(encoding="utf-8")
    assert "static-preview-note" in landing
    assert "not affiliated with the City of Cape Town" in landing
    assert "works offline" not in landing.lower()
    render = (REPO / "render.yaml").read_text(encoding="utf-8")
    assert "uvicorn fastapi_app.main:app" in render
    assert "plan: free" in render
