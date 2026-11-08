"""Build the Cloudflare Pages tree in ``dist/`` from the committed snapshot.

The Render service is not modified. Nothing here is deployed.

    python pages/build_site.py --out dist
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pages.export_timetable import (  # pylint: disable=wrong-import-position
    dump_json,
    export_day,
    export_meta,
    export_network,
    export_stops,
    open_snapshot,
    snapshot_token,
)
from pages.render_html import marquee_names, write_app_pages, write_landing  # pylint: disable=wrong-import-position

STATIC_SRC = REPO / "fastapi_app" / "static"
PAGES_JS = REPO / "pages" / "js"
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 20000

REDIRECTS = """\
/health                 /health.json            200
/search                 /search/index.html      200
/search/                /search/index.html      200
/journey                /journey/index.html     200
/journey/               /journey/index.html     200
/map                    /map/index.html         200
/map/                   /map/index.html         200
/stop/*                 /stop/index.html        200
"""

# connect-src allows the optional *.workers.dev Worker. A custom worker host
# must be added here before the browser will call it. See CLOUDFLARE_PAGES.md.
HEADERS = """\
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: SAMEORIGIN
  Permissions-Policy: geolocation=(self), microphone=(), camera=()
  Strict-Transport-Security: max-age=31536000; includeSubDomains
  Content-Security-Policy: default-src 'self'; script-src 'self' https://unpkg.com 'unsafe-inline'; style-src 'self' https://unpkg.com 'unsafe-inline'; img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; connect-src 'self' https://tile.openstreetmap.org https://*.tile.openstreetmap.org https://*.workers.dev; font-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'

/data/v/*
  Cache-Control: public, max-age=31536000, immutable

/data/ls.json
  Cache-Control: no-cache

/*.html
  Cache-Control: public, max-age=0, must-revalidate

/index.html
  Cache-Control: public, max-age=0, must-revalidate

/health.json
  Cache-Control: public, max-age=0, must-revalidate
"""


def _copy_static(out: Path) -> None:
    target = out / "static"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(STATIC_SRC, target)
    pages = target / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    for path in PAGES_JS.glob("*.mjs"):
        shutil.copy2(path, pages / path.name)
    css = REPO / "pages" / "static" / "pages.css"
    if css.exists():
        shutil.copy2(css, pages / "pages.css")


def _patch_map(out: Path, asset: str) -> None:
    path = out / "static" / "map" / "system_map.html"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "fetch('/api/map/network')",
        f"fetch('/data/v/{asset}/network.json')",
    )
    text = text.replace(
        "maybeStartPreview(map, color);",
        "maybeStartPreview(map, color, graph);",
    )
    text = text.replace(
        "function maybeStartPreview(map, colorByRoute) {",
        "function maybeStartPreview(map, colorByRoute, graph) {",
    )
    old = """  fetch("/api/trip-preview?" + q.toString())
    .then(r => { if (!r.ok) throw new Error("preview fetch failed"); return r.json(); })
    .then(data => {"""
    new = """  const ready = window.HaltePreviewReady || Promise.resolve();
  ready.then(() => {
    if (!window.HaltePreview) throw new Error("preview unavailable");
    return window.HaltePreview.buildFromQuery(graph, params);
  }).then(data => {"""
    if old not in text:
        raise RuntimeError(
            "system map preview hook not found; static patch would drop trip preview"
        )
    text = text.replace(old, new, 1)
    ready = "window.HaltePreviewReady=new Promise(function(r){window.__haltePreviewResolve=r;});"
    boot = (
        "<script>window.HALTE_DATA="
        + json.dumps(f"/data/v/{asset}")
        + ";" + ready + "</script>\n"
        + f"<script type=\"module\" src=\"/static/pages/preview.mjs?v={asset}\"></script>\n"
    )
    leaflet = "<script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\""
    text = text.replace(leaflet, boot + leaflet, 1)
    path.write_text(text, encoding="utf-8")


def _health(meta: dict) -> dict:
    return {
        "status": "ok",
        "version": meta["version"],
        "db_ready": True,
        "mode": "static",
        "timezone": meta["timezone"],
        "scraped_at": meta["scraped_at"],
        "scraped_at_display": meta["scraped_at_display"],
        "snapshot": meta["snapshot"],
    }


def _manifest(out: Path, token: str) -> dict:
    files = [path for path in out.rglob("*") if path.is_file()]
    largest = max(files, key=lambda path: path.stat().st_size)
    sizes = {str(path.relative_to(out)): path.stat().st_size for path in files}
    data_bytes = sum(size for name, size in sizes.items() if name.startswith("data/"))
    return {
        "snapshot": token,
        "files": len(files),
        "bytes": sum(sizes.values()),
        "data_bytes": data_bytes,
        "largest": {"path": str(largest.relative_to(out)), "bytes": largest.stat().st_size},
        "limits": {"max_file_bytes": MAX_FILE_BYTES, "max_files": MAX_FILES},
        "within_cloudflare_limits": (
            len(files) <= MAX_FILES and largest.stat().st_size <= MAX_FILE_BYTES
        ),
    }


def build(out: Path) -> dict:
    """Write a complete static site. Returns the manifest."""
    token = snapshot_token()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    con = open_snapshot()
    try:
        meta = export_meta(con, token)
        data = out / "data" / "v" / token
        for day in ("weekday", "saturday", "sunday"):
            dump_json(data / "timetable" / f"{day}.json", export_day(con, day))
        dump_json(data / "meta.json", meta)
        dump_json(data / "stops.json", export_stops(con))
        dump_json(data / "network.json", export_network(con))
        names = marquee_names(con)
    finally:
        con.close()
    dump_json(out / "data" / "ls.json", {
        "endpoint": None,
        "note": "Optional Worker URL. Never put ESP_API_KEY in this file.",
    })
    _copy_static(out)
    _patch_map(out, token)
    write_landing(out, token, names)
    write_app_pages(out, token, meta)
    dump_json(out / "health.json", _health(meta))
    (out / "_redirects").write_text(REDIRECTS, encoding="utf-8")
    (out / "_headers").write_text(HEADERS, encoding="utf-8")
    manifest = _manifest(out, token)
    dump_json(out / "build-manifest.json", manifest)
    if not manifest["within_cloudflare_limits"]:
        raise RuntimeError(f"Build exceeds Cloudflare Pages limits: {manifest['largest']}")
    return manifest


def main() -> None:
    """CLI entry: build into ``--out`` (default ``dist``)."""
    parser = argparse.ArgumentParser(description="Build the static Halte site")
    parser.add_argument("--out", default="dist", help="Output directory (default: dist)")
    args = parser.parse_args()
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO / out
    manifest = build(out)
    print(json.dumps({"out": str(out), **manifest}, indent=2))


if __name__ == "__main__":
    main()
