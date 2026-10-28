# Halte design notes 📐

The small decisions that make Halte behave well, collected in one place so the README can stay fun. If you change one of these, check the test that guards it (named in brackets where there is one).

---

## Brand and tokens

- **One token file.** [`fastapi_app/static/css/tokens.css`](fastapi_app/static/css/tokens.css) holds the fonts, palette, borders, radii and shadows for the landing page *and* the app. `landing.css` only aliases the shared tokens; it never redefines a brand value. *(tests/test_brand_unified.py)*
- **Palette.** Yellow `#FFE17C`, ink `#000`, charcoal `#171E19`, sage `#B7C6C2`, paper `#FFF`. Yellow is only ever a fill under ink text, or text on ink or charcoal. Every text and fill pair passes WCAG AA, and the ratios written next to the text tokens are checked against the real maths. *(test_brand_pairs_meet_wcag_aa, test_contrast_comments_in_tokens_are_honest)*
- **Fonts.** Cabinet Grotesk ExtraBold for display, Satoshi Medium for body, both self-hosted WOFF2 (no font CDN). Satoshi ships one weight, so faux bold is switched off (`font-synthesis-weight: none`) and emphasis (`strong`, labels, buttons) uses the display face instead. *(test_self_hosted_fonts_and_licence)*
- **Shape language.** 2px ink borders, 10px radii, hard offset shadows (`2px`, `4px`, `8px`, `12px`). Buttons press "into" their shadow on hover, exactly like the landing CTAs. Active nav pills, tabs and toggles are ink with yellow text, painted on the active element itself (no sliding indicator to mis-measure).
- **Hero band.** Every app page opens with a full-bleed yellow band. It's painted with `border-image` outset, so it reaches both edges of the viewport without ever widening the page.
- **Route colours** come from the official map and never change: trunk red, direct blue, area green, UCT navy.

## Pages and routing

- `/` is the landing page; journey search is `/search`. Any `/?…` query is **301**-redirected to `/search?…` with a relative `Location`. *(test_root_with_query_301_to_search)*
- The landing page copy is kept honest: no "real-time", "track your bus", stars or user counts. *(test_landing_no_banned_words)*
- Search result cards are a single tap target that opens `/journey` (the full stop list). **Preview this trip** and the ⇅ return-trip button sit above the card's tap overlay and keep their own actions. *(tests/test_journey_card_hit.py)*
- On `/journey` the **Search** header tab stays active, the back link returns to the same search, and the day reads `Weekday`, `Saturday` or `Sunday`.

## Mobile (390px and down)

- The header compacts: smaller logo mark, tighter pills, and the load-shedding badge reads `Stage 0 · default`. Below 375px the wordmark hides and the bolt mark (with its `aria-label`) stays.
- Search result cards stack as depart → `Direct · N min` → arrive. Times stay full size and never truncate; long stop names shorten with an ellipsis and carry the full name as tooltip and accessible label. The "in N min" hint moves onto the `Direct` line so both stop names share one left edge.
- Result tabs scroll inside their own pill; the page itself never scrolls sideways. Card grids use `minmax(0, 1fr)` tracks and `html` sets `-webkit-text-size-adjust: 100%`, so iOS Safari can't widen cards or inflate text.
- The hidden native `<select>` behind each stop combobox is pinned with `right: 0; max-width: 100%` so it can't widen the search card.
- Long route names wrap inside their badges (stop page and journey page) instead of pushing the page wider.

## Network map

- **Tiles** are standard OpenStreetMap (no API key), darkened with a CSS filter on the tile layer only, so route lines and stops keep their colours.
- **Stop markers** are sized by how many routes serve them: 1 to 2 routes → radius 5 in route colour; 3 to 5 → radius 6, white; 6 or more (hubs) → radius 7, white, with a permanent name label. Each has a white halo and sits in a pane above the lines. About 96% of stops are geolocated.
- **Interaction.** Desktop: hover shows the name and routes, click opens the timetable. Touch: the first tap shows the name for about 2.5 s, the second tap opens the timetable. Highlighting a route in the legend dims other stops to about 20% opacity.
- **Legend.** On phones (480px and down) it starts collapsed behind a keyboard-accessible **Legend** button (`aria-expanded`); **Esc** closes it and returns focus to the button. On desktop it's always shown.
- The map's floating controls are dark panels with yellow active states and focus rings.

## Trip preview

- Only ever started deliberately, from **Preview this trip** on an eligible result. Never from a legend highlight, a stop click or simply opening the map.
- Shows a single ghost marker interpolated from the timetable, on street geometry when available, otherwise schematic. Muted route colour at about 45% opacity with a hollow, dashed ring.
- While active, the map always shows `Estimated from timetable — not live GPS.`
- Hidden for soft-paired legs, stale scrapes, the wrong day type, or trips already past arrival. For transfers, v1 previews the first leg. Clock: Africa/Johannesburg.

## Load-shedding badge

- Screen readers hear only the `aria-label` (`Stage 0 (default)`). The visible text is `aria-hidden`, so "default" is never read twice.
- The fill colour carries the stage band (green, yellow, red) and the source text inherits the badge's own AA-safe colour.

## Caching

- Every local CSS and JS link, plus the `/map` iframe, carries `?v=<asset_v>`: the short `RENDER_GIT_COMMIT` SHA in production, the startup timestamp locally.
- HTML and `/static` responses send `Cache-Control: no-cache`, so browsers revalidate rather than serve stale CSS. Static files are not marked immutable, because unversioned URLs still work. *(tests/test_now_2_4_trust_health.py)*

## Cold start

- If `data/myciti.duckdb` is missing, the app rebuilds it from `data/snapshot/*.parquet`. It never runs the PDF scrape on boot.
- A missing snapshot is a loud startup failure, never a silently empty board.
- While the database warms up, a visible "Starting up…" banner shows instead of a blank page. `GET /health` reports `status`, `version`, `db_ready` and `scraped_at` without waiting on the ETL.
