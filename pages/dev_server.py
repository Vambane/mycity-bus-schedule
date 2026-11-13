"""Serve ``dist/`` with the same rewrites as Cloudflare ``_redirects``.

    python pages/build_site.py --out dist
    python pages/dev_server.py --port 4173
"""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]


def _file_for(path: str) -> str:
    if path == "/health":
        return "/health.json"
    if path in ("/search", "/search/"):
        return "/search/index.html"
    if path in ("/journey", "/journey/"):
        return "/journey/index.html"
    if path in ("/map", "/map/"):
        return "/map/index.html"
    if path.startswith("/stop/") and not path.endswith("/index.html"):
        return "/stop/index.html"
    return path


class PagesHandler(SimpleHTTPRequestHandler):
    """Static files plus the Pages rewrites the browser tests rely on."""

    def do_GET(self):  # noqa: N802 — stdlib name
        parts = urlsplit(self.path)
        if parts.path == "/" and parts.query:
            loc = "/search?" + parts.query
            self.send_response(301)
            self.send_header("Location", loc)
            self.end_headers()
            return
        rewritten = _file_for(parts.path)
        query = f"?{parts.query}" if parts.query else ""
        self.path = rewritten + query  # pylint: disable=attribute-defined-outside-init
        super().do_GET()

    def end_headers(self):
        """Add a couple of security headers the static host also sends."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()


def main() -> None:
    """Serve the built site until interrupted."""
    parser = argparse.ArgumentParser(description="Serve the static Halte build")
    parser.add_argument("--dir", default="dist")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    root = Path(args.dir)
    if not root.is_absolute():
        root = REPO / root
    if not (root / "index.html").exists():
        raise SystemExit(f"No build at {root}. Run pages/build_site.py first.")
    handler = partial(PagesHandler, directory=str(root))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {root} at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
