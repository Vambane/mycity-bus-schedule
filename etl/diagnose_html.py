"""
etl/diagnose_html.py — MyCiTi HTML Structure Diagnostics
==========================================================
Run this script to print the HTML structure of the MyCiTi routes page.
Paste the output back to Claude so the scraper selectors can be fixed.

Usage:
    python3 etl/diagnose_html.py
"""

import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-ZA,en;q=0.9",
}

URLS = {
    "routes":     "https://www.myciti.org.za/en/routes-stops/",
    "timetables": "https://www.myciti.org.za/en/timetables/timetable-downloads/",
}


def diagnose(label: str, url: str) -> None:
    print(f"\n{'='*70}")
    print(f"PAGE: {label}  →  {url}")
    print("="*70)

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"ERROR fetching page: {e}")
        return

    soup = BeautifulSoup(r.text, "lxml")

    # 1. All <a> tags whose href mentions routes-stops or timetable
    print("\n--- Links containing 'routes-stops' or 'timetable' ---")
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "routes-stops" in h or "timetable" in h.lower():
            print(f"  href={h!r:60s}  text={a.get_text(strip=True)[:60]!r}")

    # 2. All unique class names used in the page (helps identify containers)
    print("\n--- Top 40 most-used CSS classes ---")
    from collections import Counter
    classes = Counter()
    for tag in soup.find_all(True):
        for c in tag.get("class", []):
            classes[c] += 1
    for cls, count in classes.most_common(40):
        print(f"  .{cls:<45} ({count}x)")

    # 3. Any text that looks like a route code (T01, A01, 101 …)
    print("\n--- Text nodes that look like route IDs ---")
    route_re = re.compile(r"\b([A-Z]?\d{2,3}[A-Z]?)\b")
    seen = set()
    for tag in soup.find_all(["li", "a", "h2", "h3", "h4", "td", "span", "p"]):
        txt = tag.get_text(strip=True)
        if route_re.search(txt) and txt not in seen and len(txt) < 120:
            seen.add(txt)
            print(f"  <{tag.name}>  {txt[:100]!r}")

    # 4. Raw snippet of the <main> or <body> content (first 3000 chars)
    print("\n--- Raw HTML snippet (first 3000 chars of <main> or <body>) ---")
    main = soup.find("main") or soup.find("body")
    if main:
        # Strip script/style tags for readability
        for s in main.find_all(["script", "style"]):
            s.decompose()
        raw = str(main)[:3000]
        print(raw)


if __name__ == "__main__":
    for label, url in URLS.items():
        diagnose(label, url)

    print("\n\nDone. Paste this output back to Claude to fix the scraper selectors.")
