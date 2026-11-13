"""HTML shells for the static site. Brand chrome matches the FastAPI templates."""
# HTML templates are easier to read than wrapped lines.
# pylint: disable=line-too-long
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = Path(__file__).resolve().parent / "templates"
DISCLAIMER = (
    "Halte is an independent app, not affiliated with the City of Cape Town, "
    "MyCiTi, the University of Cape Town or PRASA (Metrorail)."
)

SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="Halte: MyCiTi, UCT Jammie shuttle and Metrorail timetables and journey planning for Cape Town">
  <meta property="og:site_name" content="Halte">
  <meta property="og:title" content="Halte">
  <meta property="og:description" content="Halte: MyCiTi, UCT Jammie shuttle and Metrorail timetables and journey planning for Cape Town">
  <title>{title}</title>
  <link rel="stylesheet" href="/static/css/tokens.css?v={asset}">
  <link rel="stylesheet" href="/static/css/base.css?v={asset}">
  <link rel="stylesheet" href="/static/css/components.css?v={asset}">
  <link rel="stylesheet" href="/static/pages/pages.css?v={asset}">
</head>
<body data-db-ready="true" data-page="{page}" data-data="{data_root}" data-asset="{asset}">
  <a class="skip-link" href="#content">Skip to content</a>
  <div id="wake-banner" class="wake-banner" role="status" hidden>
    Timetable data is loading in your browser.
  </div>
  <noscript>
    <div class="wake-banner" role="status">
      Halte's static search needs JavaScript. The timetable files are on this site;
      nothing is loaded from the Render server.
    </div>
  </noscript>
  <header class="header" role="banner">
    <div class="header-inner">
      <a href="/" class="logo" aria-label="Halte home">
        <span class="logo-mark" aria-hidden="true">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M13 3L4 14h7l-1 7 9-11h-7l1-7z" fill="#FFE17C" stroke="#FFE17C"/>
          </svg>
        </span>
        <span class="logo-word">Halte</span>
      </a>
      <nav class="nav-pills" role="navigation" aria-label="Main navigation">
        <a href="/search" class="nav-pill{search_cls}" data-nav="search"{search_cur}>Search</a>
        <a href="/map" class="nav-pill{map_cls}" data-nav="map"{map_cur}>Map</a>
      </nav>
      <span class="ls-badge ls-badge--warn" id="ls-badge" role="status"
            title="Load shedding unavailable (static)"
            aria-label="Load shedding unavailable (static)">
        <span class="ls-badge__text" aria-hidden="true">Shedding
          <span class="text-tertiary ls-badge__src ls-badge__src--long">(unavailable)</span>
          <span class="text-tertiary ls-badge__src ls-badge__src--short">&middot; n/a</span>
        </span>
      </span>
    </div>
  </header>
  <main class="main-content" id="content" role="main">
    <div class="container">
      {content}
      {trust}
    </div>
  </main>
  <footer class="footer" role="contentinfo">
    <div class="container">
      <div class="footer-inner">
        <p class="footer-sources">
          Sources: MyCiTi route PDFs · UCT shuttle timetables · PRASA Metrorail sheets, transcribed by
          <a href="https://nexttrain.co.za" target="_blank" rel="noopener">Next Train</a>
          · City of Cape Town Open Data · OpenStreetMap
        </p>
        <p class="footer-links">
          Confirm on
          <a href="https://www.myciti.org.za" target="_blank" rel="noopener">MyCiTi</a>
          &amp;
          <a href="https://uct.ac.za/students/services-transport-parking-uct-shuttle/route-maps-timetables" target="_blank" rel="noopener">UCT Transport</a>
          &amp;
          <a href="https://www.prasa.com/train-schedules/" target="_blank" rel="noopener">PRASA</a>
          &middot;
          Load shedding via <a href="https://eskomsepush.gumroad.com/l/api" target="_blank" rel="noopener">EskomSePush</a>
          (optional Worker, not called from this page until configured)
          &middot;
          Unofficial community timetable
        </p>
        <p class="footer-sources footer-disclaimer">{disclaimer}</p>
      </div>
    </div>
  </footer>
  <script src="/static/js/combobox.js?v={asset}"></script>
  <script type="module" src="/static/pages/app.mjs?v={asset}"></script>
</body>
</html>
"""


def _trust(meta: dict) -> str:
    by_op = meta.get("scraped_at_by_operator") or {}
    labels = {"myciti": "MyCiTi", "uct": "UCT", "metrorail": "Metrorail"}
    parts = " · ".join(
        f"{labels.get(name, name)} {stamp}" for name, stamp in by_op.items()
    )
    extra = f"<p class=\"trust-strip__line\">By operator: {parts}.</p>" if parts else ""
    return f"""
<aside class="trust-strip" role="note" aria-label="Data disclaimer">
  <p class="trust-strip__line">Unofficial community timetable — not affiliated with MyCiTi, UCT, PRASA (Metrorail), or the City of Cape Town.</p>
  <p class="trust-strip__line">Timetable as of {meta.get("scraped_at_display", "unknown — run ETL to refresh")}. Always confirm on
    <a href="https://www.myciti.org.za" target="_blank" rel="noopener">https://www.myciti.org.za</a>
    or
    <a href="https://uct.ac.za/students/services-transport-parking-uct-shuttle/route-maps-timetables" target="_blank" rel="noopener">https://uct.ac.za/students/services-transport-parking-uct-shuttle/route-maps-timetables</a>
    or
    <a href="https://www.prasa.com/train-schedules/" target="_blank" rel="noopener">https://www.prasa.com/train-schedules/</a>
    before you travel.</p>
  <p class="trust-strip__line">Times are from published PDFs / open data, not live GPS. Treat this as a planning aid only.</p>
  {extra}
</aside>
"""


def _shell(page: str, title: str, content: str, asset: str, meta: dict) -> str:
    search_on = page in ("search", "journey", "stop")
    return SHELL.format(
        title=title,
        asset=asset,
        page=page,
        data_root=f"/data/v/{asset}",
        search_cls=" active" if search_on else "",
        map_cls=" active" if page == "map" else "",
        search_cur=" aria-current=\"page\"" if search_on else "",
        map_cur=" aria-current=\"page\"" if page == "map" else "",
        content=content,
        disclaimer=DISCLAIMER,
        trust=_trust(meta),
    )


def write_app_pages(out: Path, asset: str, meta: dict) -> None:
    """Write search, stop, journey and map shells."""
    search = (TEMPLATES / "search_body.html").read_text(encoding="utf-8")
    pages = {
        "search/index.html": ("search", "Journey Search - Halte", search),
        "stop/index.html": (
            "stop",
            "Stop - Halte",
            "<div class=\"stop-page\" id=\"stop-root\"><p class=\"hero-subtitle\">Loading departure board…</p></div>",
        ),
        "journey/index.html": (
            "journey",
            "Journey - Halte",
            "<div class=\"journey-page\" id=\"journey-root\"><p class=\"hero-subtitle\">Loading journey…</p></div>",
        ),
        "map/index.html": (
            "map",
            "Network Map - Halte",
            """
<div class="map-page">
  <section class="hero">
    <h1 class="hero-title">Network Map</h1>
    <p class="hero-subtitle">Explore the MyCiTi bus network and Metrorail lines. Click any stop or station to view its timetable.</p>
  </section>
  <div id="ls-note"></div>
  <div class="map-frame">
    <iframe id="map-frame" title="Halte network map" loading="lazy"
            src="/static/map/system_map.html?v=__ASSET__"></iframe>
  </div>
  <div class="card" style="margin-top: var(--sp-6);">
    <div class="card-header"><h3 style="margin: 0; font-size: var(--text-md);">Legend</h3></div>
    <ul style="list-style: none; display: flex; flex-direction: column; gap: var(--sp-3);">
      <li><span class="route-badge rail">Southern Line</span> <span class="text-secondary">Metrorail lines</span></li>
      <li><span class="route-badge trunk">T01</span> <span class="text-secondary">Trunk routes</span></li>
      <li><span class="route-badge direct">216</span> <span class="text-secondary">Direct routes</span></li>
      <li><span class="route-badge area">105</span> <span class="text-secondary">Area routes</span></li>
    </ul>
  </div>
</div>
""".replace("__ASSET__", asset),
        ),
    }
    for rel, (page, title, content) in pages.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_shell(page, title, content, asset, meta), encoding="utf-8")


def write_landing(out: Path, asset: str, marquee: list[str]) -> None:
    """Render the existing landing template with the static-preview note."""
    env = Environment(
        loader=FileSystemLoader(str(REPO / "fastapi_app" / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("pages/landing.html").render(
        asset_v=asset,
        static_pages=True,
        marquee_routes=marquee,
        app_disclaimer=DISCLAIMER,
    )
    (out / "index.html").write_text(html, encoding="utf-8")


def marquee_names(con) -> list[str]:
    """Same route order as fastapi_app/routes/landing.py."""
    rows = con.execute(
        """
        SELECT route_id, route_name
        FROM routes
        ORDER BY CASE WHEN operator = 'myciti' THEN 0 ELSE 1 END, route_id
        """
    ).fetchall()
    return [(name or "").strip() or str(route_id) for route_id, name in rows]
