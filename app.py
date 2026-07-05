"""
app.py — MyCiTi Bus Timetable (DuckDB edition)
===============================================
Streamlit app that reads live scraped data from data/myciti.duckdb.

Run:
    streamlit run app.py

Requires the ETL to have been run first:
    python3 run_etl.py
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import streamlit as st
import duckdb
import pandas as pd

from system_map import build_network, render_system_map

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


@st.cache_data(ttl=300)
def search_stops(query: str) -> pd.DataFrame:
    """
    Return stops whose name contains the query (case-insensitive).

    Queries the stops table, de-duplicated on stop_name since the same
    physical stop can appear under multiple routes.
    """
    con = get_connection()
    return con.execute(
        """
        SELECT DISTINCT stop_name
        FROM   stops
        WHERE  stop_name ILIKE ?
        ORDER  BY stop_name
        """,
        [f"%{query.strip()}%"],
    ).df()


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
        day_type:     Streamlit radio label string.
        current_time: HH:MM:SS string for the current wall-clock time.

    Returns:
        DataFrame with [route_id, route_name, direction, departure_time],
        sorted by route then departure_time ascending.
    """
    con = get_connection()

    # Map Streamlit radio label → DB day_type value
    db_day = DAY_LABEL_TO_DB.get(day_type, "weekday")

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
        [stop_name, db_day, current_time],
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
    except Exception:
        pass
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
    # its key; the search box (key="stop_query") can only be pre-filled
    # before it is instantiated, so this must run ahead of the tabs.
    clicked = st.session_state.get("system_map")
    if clicked and clicked != st.session_state.get("_last_map_click"):
        st.session_state["_last_map_click"] = clicked
        st.session_state["stop_query"] = clicked

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


def render_search_tab() -> None:
    """Find-a-Stop tab: search → select → day filter → map + departures."""

    # key="stop_query" lets a system-map click inject a stop name here
    query = st.text_input(
        "Type a stop name",
        key="stop_query",
        placeholder="e.g. Civic Centre, Blouberg, Table View …",
    )

    if not query.strip():
        st.info("Start typing a stop name above to begin.")
        return

    matching = search_stops(query)

    if matching.empty:
        st.warning("No stops found. Try a different search term.")
        return

    # --- Stop selector ---
    selected_stop = st.selectbox(
        "Select stop",
        options=matching["stop_name"].tolist(),
    )

    # --- Day type ---
    # Default the radio to today's actual day type in Cape Town.
    # (Public holidays follow the Sunday timetable but are not auto-detected.)
    weekday_idx = datetime.now(LOCAL_TZ).weekday()  # 0=Mon … 6=Sun
    default_day = 0 if weekday_idx < 5 else (1 if weekday_idx == 5 else 2)
    day_type = st.radio(
        "Day",
        options=["Weekday (Mon–Fri)", "Saturday", "Sunday / Public holiday"],
        index=default_day,
        horizontal=True,
    )
    st.caption("Public holidays follow the Sunday timetable.")

    st.markdown("---")

    # --- Routes at this stop ---
    st.subheader(f"📍 {selected_stop}")
    routes_here = get_routes_for_stop(selected_stop)

    if routes_here.empty:
        st.write("No route information found for this stop.")
    else:
        route_list = ", ".join(routes_here["route_name"].tolist())
        st.write(f"**Routes serving this stop:** {route_list}")

    st.markdown("---")

    # --- Departure map: the whole day at a glance ---
    # Cape Town wall-clock time, independent of the server's timezone
    now = datetime.now(LOCAL_TZ)
    now_str = now.strftime("%H:%M:%S")

    day_departures = get_day_departures(
        selected_stop, DAY_LABEL_TO_DB.get(day_type, "weekday")
    )
    if not day_departures.empty:
        st.subheader(f"🗺️ Departure Map · {day_type}")
        now_hours = now.hour + now.minute / 60
        render_departure_map(day_departures, now_hours)
        st.markdown("---")
    st.subheader(f"🕐 Upcoming Departures · {day_type}")
    st.caption(f"Current time: {now_str[:5]}")

    upcoming = get_upcoming_departures(selected_stop, day_type, now_str)

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
