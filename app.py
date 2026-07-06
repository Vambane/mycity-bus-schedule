"""
app.py — MyCiTi Bus Timetable (DuckDB edition)
===============================================
Streamlit app that reads live scraped data from data/myciti.duckdb.

Run:
    streamlit run app.py

Requires the ETL to have been run first:
    python3 run_etl.py
"""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import streamlit as st
import duckdb
import pandas as pd

from journey import find_connections
from system_map import build_network, render_system_map

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "myciti.duckdb"
SNAPSHOT_DIR = Path(__file__).parent / "data" / "snapshot"
MAX_DEPARTURES = 10

# MyCiTi operates in Cape Town — pin the timezone so "upcoming departures"
# are correct regardless of where the app server runs.
LOCAL_TZ = ZoneInfo("Africa/Johannesburg")

# Streamlit radio label → departures.day_type value
DAY_LABEL_TO_DB = {
    "Weekday (Mon–Fri)": "weekday",
    "Saturday": "saturday",
    "Sunday / Public holiday": "sunday",
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _build_db_from_snapshot() -> None:
    """
    Create the DuckDB file from the Parquet snapshot committed to the repo.

    The .duckdb file itself is gitignored, so on a fresh deployment (e.g.
    Streamlit Community Cloud) it doesn't exist. The ETL exports every table
    to data/snapshot/*.parquet, and this rebuilds the database from them in
    well under a second — no scraping needed at boot.
    """
    con = duckdb.connect(str(DB_PATH))
    try:
        for pq in sorted(SNAPSHOT_DIR.glob("*.parquet")):
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {pq.stem} AS "
                "SELECT * FROM read_parquet(?)",
                [str(pq)],
            )
    finally:
        con.close()


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    """
    Open a read-only DuckDB connection (cached for the Streamlit session).

    If the database file is missing, it is first rebuilt from the committed
    Parquet snapshot; the ETL is only required when no snapshot exists.
    """
    if not DB_PATH.exists():
        if any(SNAPSHOT_DIR.glob("*.parquet")):
            _build_db_from_snapshot()
        else:
            raise FileNotFoundError(
                f"Database not found at {DB_PATH} and no snapshot in "
                f"{SNAPSHOT_DIR}.\nPlease run the ETL first:\n\n    python3 run_etl.py"
            )
    return duckdb.connect(str(DB_PATH), read_only=True)


@st.cache_data(ttl=300)
def load_routes() -> pd.DataFrame:
    """Load all routes from DuckDB."""
    con = get_connection()
    return con.execute("SELECT route_id, route_name FROM routes ORDER BY route_id").df()


@st.cache_data(ttl=3600)
def get_stop_names() -> list[str]:
    """All stop names with departures, sorted — the From/To selector options."""
    con = get_connection()
    return [
        row[0] for row in
        con.execute("SELECT DISTINCT stop_name FROM departures ORDER BY 1").fetchall()
    ]


@st.cache_data(ttl=300)
def get_connections(from_stop: str, to_stop: str, day_type: str) -> pd.DataFrame:
    """Cached direct-connection lookup between two stops (see journey.py)."""
    return find_connections(get_connection(), from_stop, to_stop, day_type)


@st.cache_data(ttl=300)
def get_routes_for_stop(stop_name: str) -> pd.DataFrame:
    """
    Return all routes that serve a given stop name.

    Join: stops → routes
    """
    con = get_connection()
    return con.execute(
        """
        SELECT DISTINCT r.route_id, r.route_name
        FROM   stops   s
        JOIN   routes  r ON r.route_id = s.route_id
        WHERE  s.stop_name = ?
        ORDER  BY r.route_id
        """,
        [stop_name],
    ).df()


@st.cache_data(ttl=60)
def get_upcoming_departures(
    stop_name: str,
    day_type: str,
    current_time: str,
) -> pd.DataFrame:
    """
    Return upcoming departure times for a stop on a given day type.

    Args:
        stop_name:    Exact stop name.
        day_type:     DB day_type value ('weekday' | 'saturday' | 'sunday').
        current_time: HH:MM:SS string for the current wall-clock time.

    Returns:
        DataFrame with [route_id, route_name, direction, departure_time],
        sorted by route then departure_time ascending.
    """
    con = get_connection()

    # DISTINCT guards against duplicate rows from overlapping PDF tables;
    # string comparison on zero-padded HH:MM:SS is chronologically correct
    # (including GTFS overnight times like 24:15:00 > 23:59:59).
    return con.execute(
        """
        SELECT  DISTINCT
                d.route_id,
                r.route_name,
                d.direction,
                d.departure_time
        FROM    departures d
        JOIN    routes     r ON r.route_id = d.route_id
        WHERE   d.stop_name  = ?
          AND   d.day_type   = ?
          AND   d.departure_time > ?
        ORDER   BY d.route_id, d.direction, d.departure_time
        """,
        [stop_name, day_type, current_time],
    ).df()


@st.cache_data(ttl=300)
def get_day_departures(stop_name: str, day_type: str) -> pd.DataFrame:
    """
    Return ALL departures for a stop on a given day type (no future filter).

    Used by the departure map, which shows the whole day so the service
    pattern (peak frequency, first/last bus) is visible at a glance.

    Args:
        stop_name: Exact stop name.
        day_type:  DB day_type value ('weekday' | 'saturday' | 'sunday').

    Returns:
        DataFrame with [route_id, route_name, direction, departure_time],
        sorted by route then direction then time.
    """
    con = get_connection()
    return con.execute(
        """
        SELECT  DISTINCT
                d.route_id,
                r.route_name,
                d.direction,
                d.departure_time
        FROM    departures d
        JOIN    routes     r ON r.route_id = d.route_id
        WHERE   d.stop_name = ?
          AND   d.day_type  = ?
        ORDER   BY d.route_id, d.direction, d.departure_time
        """,
        [stop_name, day_type],
    ).df()


@st.cache_data(ttl=3600)
def load_network() -> dict:
    """
    Build (and cache) the geographic system-map graph (v2: nodes carry
    lat/lon, plus street "paths" and geolocation "coverage").
    """
    return build_network(get_connection())


def get_scrape_info() -> str:
    """Return a human-readable summary of the last ETL run."""
    try:
        con = get_connection()
        row = con.execute(
            """
            SELECT finished_at, routes_loaded, stops_loaded, departures_loaded
            FROM   scrape_log
            ORDER  BY run_id DESC
            LIMIT  1
            """
        ).fetchone()
        if row:
            return (
                f"Last updated: **{str(row[0])[:16]}** UTC  |  "
                f"{row[1]} routes · {row[2]} stops · {row[3]} departures"
            )
    except Exception as exc:  # scrape_log is informational; log but don't crash the UI
        log.warning("Could not read scrape_log: %s", exc)
    return ""


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _time_to_hours(time_str: pd.Series) -> pd.Series:
    """Convert 'HH:MM:SS' strings to decimal hours (e.g. '06:30:00' → 6.5)."""
    parts = time_str.str.split(":", expand=True).astype(int)
    return parts[0] + parts[1] / 60


def render_departure_map(day_departures: pd.DataFrame, now_hours: float) -> None:
    """
    Render a timeline "map" of every departure of the day for the stop.

    One row per route + direction; each tick is one departure. A dashed
    rule marks the current time, so peak frequency, service gaps and the
    first/last bus are all visible at a glance.
    """
    df = day_departures.copy()
    df["hour"] = _time_to_hours(df["departure_time"])
    # Direction strings already embed the route id ("To 101 Vredehoek"),
    # so they double as unique, readable row labels
    df["line"] = df["direction"]
    df["Time"] = df["departure_time"].str[:5]

    # Pad the x-axis half an hour either side of the day's actual service span
    lo = max(0.0, df["hour"].min() - 0.5)
    hi = df["hour"].max() + 0.5
    axis_ticks = list(range(int(lo), int(hi) + 2, 2))

    ticks = (
        alt.Chart(df)
        .mark_tick(thickness=2, size=14)
        .encode(
            x=alt.X(
                "hour:Q",
                scale=alt.Scale(domain=[lo, hi]),
                axis=alt.Axis(
                    values=axis_ticks,
                    # Display 25:30 (GTFS overnight) as 01:30 on the axis
                    labelExpr="format(datum.value % 24, '02d') + ':00'",
                ),
                title="Time of day",
            ),
            y=alt.Y("line:N", title=None, sort=None),
            color=alt.Color("route_id:N", legend=None),
            tooltip=[
                alt.Tooltip("route_name:N", title="Route"),
                alt.Tooltip("direction:N", title="Direction"),
                alt.Tooltip("Time:N", title="Departs"),
            ],
        )
    )

    # Dashed rule at the current wall-clock time in Cape Town
    now_rule = (
        alt.Chart(pd.DataFrame({"hour": [now_hours]}))
        .mark_rule(color="red", strokeDash=[5, 4])
        .encode(x="hour:Q")
    )

    st.altair_chart(ticks + now_rule, use_container_width=True)
    st.caption("Each tick is one scheduled departure · red line = current time")


def render_route_block(route_name: str, departures: pd.DataFrame) -> None:
    """
    Render the timetable card for one route.

    Departures are grouped by direction so the "next departure" metric is
    meaningful — mixing both travel directions would show the next bus
    regardless of where it is headed.
    """
    st.markdown(f"#### 🚌 {route_name}")

    if departures.empty:
        st.info("No more buses today on this route.")
        st.markdown("---")
        return

    for direction in departures["direction"].unique():
        dir_deps = departures[departures["direction"] == direction]

        st.markdown(f"**{direction}**")

        # Next departure metric (first row — already sorted ascending)
        next_time = str(dir_deps.iloc[0]["departure_time"])[:5]  # HH:MM
        st.metric("Next departure", next_time)

        # Full table (capped at MAX_DEPARTURES per direction)
        display = dir_deps.head(MAX_DEPARTURES).copy()
        display["Time"] = display["departure_time"].str[:5]

        st.dataframe(
            display[["Time"]].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("---")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:
    """MyCiTi Bus Timetable — Streamlit entry point."""

    st.set_page_config(
        page_title="MyCiTi Bus Timetable",
        page_icon="🚌",
        layout="centered",
    )

    st.title("🚌 MyCiTi Bus Timetable")
    st.caption("Western Cape · Data sourced from myciti.org.za")

    # --- Connection check ---
    try:
        get_connection()
    except FileNotFoundError as err:
        st.error(str(err))
        st.code("python3 run_etl.py", language="bash")
        st.stop()

    # Show last ETL run info
    info = get_scrape_info()
    if info:
        st.markdown(info)

    st.markdown("---")

    # --- Handle a system-map stop click BEFORE any widget renders ---
    # The map component stores its last clicked stop in session state under
    # its key; the origin selector (key="from_stop") can only be pre-filled
    # before it is instantiated, so this must run ahead of the tabs.
    clicked = st.session_state.get("system_map")
    if clicked and clicked != st.session_state.get("_last_map_click"):
        st.session_state["_last_map_click"] = clicked
        st.session_state["from_stop"] = clicked

    # --- Tabs: stop search / interactive system map ---
    tab_search, tab_map = st.tabs(["🔍 Find a Stop", "🗺️ System Map"])
    with tab_search:
        render_search_tab()
    with tab_map:
        render_map_tab()

    # --- Refresh button ---
    st.markdown("---")
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()


def render_map_tab() -> None:
    """System Map tab: interactive network diagram of every route."""
    graph = load_network()
    cov = graph["coverage"]
    st.caption(
        f"The MyCiTi network over Cape Town — {cov['located']} of {cov['total']} "
        "stops placed at their true location (City of Cape Town open data). "
        "Toggle between square-ish schematic lines and exact street paths; "
        "click a route in the legend to highlight it, click a stop for its timetable."
    )
    clicked = render_system_map(graph)

    if clicked:
        st.info(f"Showing **{clicked}** in the *Find a Stop* tab.")


def _swap_stops() -> None:
    """Swap the From/To selections (the ⇄ button's on_click callback)."""
    st.session_state["from_stop"], st.session_state["to_stop"] = (
        st.session_state.get("to_stop"),
        st.session_state.get("from_stop"),
    )


def render_search_tab() -> None:
    """
    Find-a-Stop tab, laid out like a flight search: a From → To search bar
    with a day selector on top, journey result cards below. Leaving "Going
    to" empty shows the classic single-stop departure board instead.
    """
    stops = get_stop_names()

    # --- search bar ---
    with st.container(border=True):
        c_from, c_swap, c_to, c_day = st.columns([5, 1, 5, 4])
        from_stop = c_from.selectbox(
            "🚏 Leaving from",
            options=stops,
            index=None,
            placeholder="Choose a stop …",
            key="from_stop",
        )
        c_swap.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)
        c_swap.button("⇄", on_click=_swap_stops, help="Swap From and To",
                      use_container_width=True)
        to_stop = c_to.selectbox(
            "🎯 Going to",
            options=stops,
            index=None,
            placeholder="Any — show all departures",
            key="to_stop",
        )
        # Default the day to today's actual day type in Cape Town
        weekday_idx = datetime.now(LOCAL_TZ).weekday()  # 0=Mon … 6=Sun
        default_day = 0 if weekday_idx < 5 else (1 if weekday_idx == 5 else 2)
        day_type = c_day.selectbox(
            "📅 Day",
            options=list(DAY_LABEL_TO_DB),
            index=default_day,
            key="day_type",
        )
        st.caption("Public holidays follow the Sunday timetable · "
                   "all MyCiTi connections shown are direct services")

    if not from_stop:
        st.info("Choose a departure stop to begin — or click one on the System Map.")
        return

    if to_stop and to_stop != from_stop:
        _render_journey_results(from_stop, to_stop, day_type)
    else:
        _render_single_stop(from_stop, day_type)


def _chips(labels: list[tuple[str, str, str]]) -> str:
    """Render badge chips like 'Best'/'Fastest' as inline HTML spans."""
    return "".join(
        f'<span style="background:{bg};color:{fg};padding:2px 10px;'
        f'border-radius:12px;font-size:0.78rem;font-weight:600;'
        f'margin-right:6px">{label}</span>'
        for label, bg, fg in labels
    )


def _render_connection_card(
    row: pd.Series,
    color: str,
    badges: list[tuple[str, str, str]],
    from_stop: str,
    to_stop: str,
) -> None:
    """One journey result card: route · departure → duration → arrival."""
    with st.container(border=True):
        if badges:
            st.markdown(_chips(badges), unsafe_allow_html=True)
        c_route, c_dep, c_mid, c_arr = st.columns([4, 2, 3, 2])

        short_name = row["route_name"].split("– ", 1)[-1]
        c_route.markdown(
            f'<span style="color:{color};font-weight:800;font-size:1.2rem">'
            f'{row["route_id"]}</span><br>'
            f'<span style="color:#888;font-size:0.85rem">{short_name}</span>',
            unsafe_allow_html=True,
        )
        c_dep.markdown(f"### {row['dep'][:5]}")
        c_dep.caption(from_stop)
        c_mid.markdown(
            '<div style="text-align:center;margin-top:10px">'
            '<span style="color:#bbb">○──</span>'
            '<span style="background:#188038;color:#fff;padding:2px 12px;'
            'border-radius:10px;font-size:0.8rem;font-weight:700">Direct</span>'
            '<span style="color:#bbb">──○</span>'
            f'<div style="color:#888;font-size:0.85rem;margin-top:2px">'
            f'{row["duration"]}</div></div>',
            unsafe_allow_html=True,
        )
        c_arr.markdown(f"### {row['arr'][:5]}")
        c_arr.caption(to_stop)


def _render_journey_results(from_stop: str, to_stop: str, day_type: str) -> None:
    """Journey view: sidebar filters + Best / Fastest / All-day result tabs."""
    colors = {r["id"]: r["color"] for r in load_network()["routes"]}
    conns = get_connections(from_stop, to_stop, DAY_LABEL_TO_DB[day_type])

    if conns.empty:
        st.warning(
            f"No direct services from **{from_stop}** to **{to_stop}** on a "
            f"{day_type.lower()}. Try swapping the direction (⇄) — or the trip "
            "may need a transfer, which isn't supported yet."
        )
        return

    # --- sidebar filters (like the airline checkboxes) ---
    st.sidebar.header("Filters")
    route_counts = conns["route_id"].value_counts()
    st.sidebar.subheader("Routes")
    active = [
        rid for rid, count in route_counts.items()
        if st.sidebar.checkbox(f"{rid} · {count} trips", value=True, key=f"flt_{rid}")
    ]
    conns = conns[conns["route_id"].isin(active)]

    now_str = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    upcoming = conns[conns["dep"] > now_str]
    st.sidebar.caption(f"Showing {len(upcoming)} of {len(conns)} departures")

    st.subheader(f"📍 {from_stop} → {to_stop} · {day_type}")
    st.caption(f"Current time in Cape Town: {now_str[:5]}")

    if upcoming.empty:
        st.info("No more departures today — the All day tab shows the full timetable.")
    fastest_min = int(conns["duration_min"].min()) if not conns.empty else 0

    tab_best, tab_fast, tab_all = st.tabs(["⭐ Best", "⚡ Fastest", "🗓️ All day"])

    with tab_best:
        fastest_badged = False
        for i, (_, row) in enumerate(upcoming.head(8).iterrows()):
            badges = [("Best", "#e7f0fe", "#1a73e8")] if i == 0 else []
            # Badge "Fastest" only on the first card achieving the best time
            if row["duration_min"] == fastest_min and not fastest_badged:
                fastest_badged = True
                badges.append(("Fastest", "#e6f4ea", "#137333"))
            _render_connection_card(
                row, colors.get(row["route_id"], "#666"), badges, from_stop, to_stop)

    with tab_fast:
        by_speed = upcoming.sort_values(["duration_min", "dep"]).head(8)
        for i, (_, row) in enumerate(by_speed.iterrows()):
            badges = [("Fastest", "#e6f4ea", "#137333")] if i == 0 else []
            _render_connection_card(
                row, colors.get(row["route_id"], "#666"), badges, from_stop, to_stop)

    with tab_all:
        display = conns.rename(columns={
            "dep": "Departs", "arr": "Arrives",
            "duration": "Duration", "route_id": "Route", "direction": "Direction",
        }).copy()
        display["Departs"] = display["Departs"].str[:5]
        display["Arrives"] = display["Arrives"].str[:5]
        st.dataframe(
            display[["Departs", "Arrives", "Duration", "Route", "Direction"]],
            use_container_width=True, hide_index=True,
        )


def _render_single_stop(selected_stop: str, day_type: str) -> None:
    """Classic single-stop departure board (no destination selected)."""
    # Map the UI label once; pass the DB value to all query helpers.
    db_day = DAY_LABEL_TO_DB.get(day_type, "weekday")

    st.subheader(f"📍 {selected_stop}")
    routes_here = get_routes_for_stop(selected_stop)

    if routes_here.empty:
        st.write("No route information found for this stop.")
    else:
        route_list = ", ".join(routes_here["route_name"].tolist())
        st.write(f"**Routes serving this stop:** {route_list}")

    st.markdown("---")

    # Cape Town wall-clock time, independent of the server's timezone
    now = datetime.now(LOCAL_TZ)
    now_str = now.strftime("%H:%M:%S")

    day_departures = get_day_departures(selected_stop, db_day)
    if not day_departures.empty:
        st.subheader(f"🗺️ Departure Map · {day_type}")
        render_departure_map(day_departures, now.hour + now.minute / 60)
        st.markdown("---")
    st.subheader(f"🕐 Upcoming Departures · {day_type}")
    st.caption(f"Current time: {now_str[:5]}")

    upcoming = get_upcoming_departures(selected_stop, db_day, now_str)

    if upcoming.empty:
        st.info(
            "No upcoming buses at this stop for the selected day. "
            "Try a different day or search for another stop."
        )
    else:
        for route_name in upcoming["route_name"].unique():
            route_deps = upcoming[upcoming["route_name"] == route_name].copy()
            render_route_block(route_name, route_deps)


if __name__ == "__main__":
    main()
