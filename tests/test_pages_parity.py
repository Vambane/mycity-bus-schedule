"""Static planner versus the Python timetable oracle."""
from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from fastapi_app.services.journey_page import encode_legs
from fastapi_app.services.service_day import (
    DAY_LABELS,
    day_type_for,
    holiday_name,
    is_live_board,
    next_service_day,
    overnight_carryover,
)
from fastapi_app.services.trip_window import arriving_by, last_departure_by, parse_window
from fastapi_app.services.uct_validity import uct_expiry_state
from journey import clock_time, find_connections, find_transfer_connections
from journey_map import leg_stop_sequence
from pages.export_timetable import open_snapshot

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "pages" / "js" / "parity_runner.mjs"


@pytest.fixture(scope="module")
def built():
    """One static build for every parity case."""
    subprocess.run(
        ["python3", "pages/build_site.py", "--out", "dist"],
        cwd=REPO, check=True,
    )
    manifest = json.loads((REPO / "dist" / "build-manifest.json").read_text(encoding="utf-8"))
    data = REPO / "dist" / "data" / "v" / manifest["snapshot"]
    return data


@pytest.fixture(scope="module")
def con():
    connection = open_snapshot()
    yield connection
    connection.close()


def _node(data_dir: Path, cases: list[dict]) -> list[dict]:
    cases_path = REPO / "dist" / "parity-cases.json"
    cases_path.write_text(json.dumps({"dataDir": str(data_dir), "cases": cases}), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(RUNNER), str(cases_path)],
        text=True,
        capture_output=True,
        cwd=REPO,
        check=False,
        timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr[-5000:] or proc.stdout[-2000:])
    return json.loads(proc.stdout)


def _direct(rows):
    out = []
    for row in rows or []:
        out.append({
            "route_id": row["route_id"] if isinstance(row, dict) else row.route_id,
            "direction": row["direction"] if isinstance(row, dict) else row.direction,
            "dep": row["dep"] if isinstance(row, dict) else row.dep,
            "arr": row["arr"] if isinstance(row, dict) else row.arr,
            "duration_min": int(row["duration_min"] if isinstance(row, dict) else row.duration_min),
        })
    return out


def _legs(legs):
    cleaned = []
    for leg in list(legs):
        cleaned.append({
            "route_id": leg["route_id"],
            "direction": leg["direction"],
            "board": leg["board"],
            "alight": leg["alight"],
            "dep": leg["dep"],
            "arr": leg["arr"],
            "wait_min": int(leg["wait_min"]),
        })
    return cleaned


def _transfers(rows):
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            row = row.to_dict()
        out.append({
            "dep": row["dep"],
            "arr": row["arr"],
            "duration_min": int(row["duration_min"]),
            "transfers": int(row["transfers"]),
            "route_ids": list(row["route_ids"]),
            "via": list(row["via"]),
            "legs": _legs(row["legs"]),
        })
    return out


def _js_direct(result):
    return _direct(result["direct"])


def _js_transfers(result):
    return _transfers(result["transfers"])


def _clock_window(now, day_type, search_iso, when, at):
    """Leave-now / depart / arrive, matching the search route's clock rules."""
    window = parse_window(when, at)
    live = is_live_board(day_type, now, search_iso)
    current = now.strftime("%H:%M:%S") if live else "00:00:00"
    on_today = live
    arrive_floor = current
    if window.mode != "now":
        live = False
        current = window.at if window.mode == "depart" else "00:00:00"
    return window, live, current, on_today, arrive_floor


def _direct_frame(con, spec, expired, tomorrow_type):
    """Direct rows after the same window the search route applies."""
    from fastapi_app.routes.search import (  # pylint: disable=import-outside-toplevel
        _in_clock_order,
        _with_overnight_carryover,
        _without_expired_direct,
    )

    direct = _without_expired_direct(
        find_connections(
            con, spec["from_stop"], spec["to_stop"], spec["day_type"], spec["operator"],
        ),
        expired,
    )
    if spec["live_for_overnight"]:
        direct = _with_overnight_carryover(
            direct, con, spec["from_stop"], spec["to_stop"], spec["operator"], expired, spec["now"],
        )
    results_day = spec["day_type"]
    showing_next = False
    arrive_missed = None
    if spec["window"].mode == "arrive":
        if spec["on_today"]:
            arrive_missed = last_departure_by(direct, spec["window"].at)
        direct = arriving_by(direct, spec["window"].at, spec["arrive_floor"])
    elif direct is not None and not direct.empty:
        upcoming = direct[direct["dep"] >= spec["current"]]
        if not upcoming.empty:
            direct = _in_clock_order(upcoming)
        else:
            if tomorrow_type != spec["day_type"]:
                direct = _without_expired_direct(
                    find_connections(
                        con, spec["from_stop"], spec["to_stop"], tomorrow_type, spec["operator"],
                    ),
                    expired,
                )
            direct = direct.head(10)
            if not direct.empty:
                showing_next = True
                results_day = tomorrow_type
    return direct, results_day, showing_next, arrive_missed


def _transfer_frame(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    con, spec, expired, tomorrow_type, arrive_missed, results_day, showing_next,
):
    """Transfer rows, only when no direct row survived."""
    from fastapi_app.routes.search import (  # pylint: disable=import-outside-toplevel
        _without_expired_transfers,
    )

    transfers = _without_expired_transfers(
        find_transfer_connections(
            con, spec["from_stop"], spec["to_stop"], spec["day_type"], operator=spec["operator"],
        ),
        expired,
    )
    if spec["window"].mode == "arrive":
        if spec["on_today"] and arrive_missed is None:
            arrive_missed = last_departure_by(transfers, spec["window"].at)
        transfers = arriving_by(transfers, spec["window"].at, spec["arrive_floor"])
    elif transfers is not None and not transfers.empty:
        upcoming = transfers[transfers["dep"] >= spec["current"]]
        if not upcoming.empty:
            transfers = upcoming.sort_values("dep").reset_index(drop=True)
        else:
            if tomorrow_type != spec["day_type"]:
                transfers = _without_expired_transfers(
                    find_transfer_connections(
                        con, spec["from_stop"], spec["to_stop"], tomorrow_type,
                        operator=spec["operator"],
                    ),
                    expired,
                )
            transfers = transfers.head(10)
            if not transfers.empty:
                showing_next = True
                results_day = tomorrow_type
    return transfers, results_day, showing_next, arrive_missed


def oracle_search(  # pylint: disable=too-many-arguments
    con, *, now, from_stop, to_stop, day_type=None, operator="both",
    when=None, at=None, search_date=None,
):
    """Same filtering as fastapi_app/routes/search.py, with an injected clock."""
    search_iso = None
    if search_date:
        search_iso = datetime.strptime(search_date, "%Y-%m-%d").date()
        if day_type is None:
            day_type = day_type_for(search_iso)
    if day_type is None:
        day_type = day_type_for(now.date())
    if day_type not in ("weekday", "saturday", "sunday"):
        day_type = "weekday"
    if operator not in ("both", "myciti", "uct", "metrorail"):
        operator = "both"
    window, live, current, on_today, arrive_floor = _clock_window(
        now, day_type, search_iso, when, at,
    )
    expired: set[str] = set()
    if operator in ("uct", "both"):
        expired, _warning = uct_expiry_state(con, now.date())
    spec = {
        "from_stop": from_stop,
        "to_stop": to_stop,
        "day_type": day_type,
        "operator": operator,
        "now": now,
        "window": window,
        "live_for_overnight": live,
        "current": current,
        "on_today": on_today,
        "arrive_floor": arrive_floor,
    }
    tomorrow, tomorrow_type = next_service_day(now)
    direct, results_day, showing_next, arrive_missed = _direct_frame(
        con, spec, expired, tomorrow_type,
    )
    transfers = None
    if direct is None or direct.empty:
        transfers, results_day, showing_next, arrive_missed = _transfer_frame(
            con, spec, expired, tomorrow_type, arrive_missed, results_day, showing_next,
        )
    direct_rows = [] if direct is None or direct.empty else direct.to_dict("records")
    transfer_rows = [] if transfers is None or transfers.empty else transfers.to_dict("records")
    return {
        "direct": _direct(direct_rows),
        "transfers": _transfers(transfer_rows),
        "showing_next_day": showing_next,
        "results_day_type": results_day,
        "live": live,
        "arrive_missed": clock_time(arrive_missed) if arrive_missed else None,
        "has_results": bool(direct_rows or transfer_rows),
        "next_day_label": DAY_LABELS[results_day],
        "next_day_holiday": holiday_name(tomorrow) if showing_next else None,
    }


def _trip_key(row):
    return (row["dep"], row["route_id"], row["direction"], row["arr"], row["duration_min"])


def _leg_key(row):
    return tuple(
        (
            leg["route_id"], leg["direction"], leg["board"], leg["alight"],
            leg["dep"], leg["arr"], int(leg["wait_min"]),
        )
        for leg in row["legs"]
    )


def _raw_transfers(con, from_stop, to_stop, day_type, operator):
    """Every transfer itinerary before the Pareto cut.

    DuckDB does not order line scans, so two itineraries with the same
    departure, arrival and transfer count can swap which one survives.
    Both are valid under journey._pareto. The static planner breaks those
    ties by route id because its lines are sorted.
    """
    from journey import (  # pylint: disable=import-outside-toplevel
        _load_lines,
        _one_transfer_rows,
        _two_transfer_rows,
    )

    lines = _load_lines(con, day_type, operator)
    names = dict(con.execute("SELECT route_id, route_name FROM routes").fetchall())
    lines_from = {key: value for key, value in lines.items() if from_stop in value}
    lines_to = {key: value for key, value in lines.items() if to_stop in value}
    rows = _one_transfer_rows(lines_from, lines_to, from_stop, to_stop, names)
    if not rows:
        rows = _two_transfer_rows(lines, lines_from, lines_to, from_stop, to_stop, names)
    return rows


def _compare_search(con, data, **query):
    now = datetime.fromisoformat(query["now"])
    fields = {k: v for k, v in query.items() if k != "now"}
    expected = oracle_search(con, now=now, **fields)
    got = _node(data, [{
        "id": "case",
        "kind": "search",
        "now": query["now"],
        "from_stop": query["from_stop"],
        "to_stop": query["to_stop"],
        "day_type": query.get("day_type"),
        "operator": query.get("operator", "both"),
        "when": query.get("when"),
        "at": query.get("at"),
        "search_date": query.get("search_date"),
    }])[0]["result"]
    assert sorted(map(_trip_key, _js_direct(got))) == sorted(map(_trip_key, expected["direct"]))
    js_transfers = _js_transfers(got)
    py_transfers = expected["transfers"]
    assert [row["dep"] for row in js_transfers] == [row["dep"] for row in py_transfers]
    for js_row, py_row in zip(js_transfers, py_transfers):
        assert js_row["arr"] == py_row["arr"]
        assert js_row["duration_min"] == py_row["duration_min"]
        assert js_row["transfers"] == py_row["transfers"]
        assert len(js_row["legs"]) == len(py_row["legs"])
    if js_transfers:
        day = got["results_day_type"]
        allowed = {_leg_key(row) for row in _raw_transfers(
            con, query["from_stop"], query["to_stop"], day, query.get("operator", "both"),
        )}
        for row in js_transfers:
            assert _leg_key(row) in allowed
    for key in ("showing_next_day", "results_day_type", "live", "arrive_missed", "has_results",
                "next_day_label", "next_day_holiday"):
        assert got[key] == expected[key], key


def test_calendar_matches_python_through_2030():
    cases = []
    day = date(2024, 1, 1)
    while day <= date(2030, 12, 31):
        cases.append({"id": day.isoformat(), "kind": "day", "date": day.isoformat()})
        day += timedelta(days=1)
    for year in range(2020, 2036):
        cases.append({"id": f"easter-{year}", "kind": "easter", "year": year})
    # No timetable files required for calendar cases; give an empty catalog.
    payload_path = REPO / "dist"
    payload_path.mkdir(exist_ok=True)
    path = payload_path / "calendar-cases.json"
    path.write_text(json.dumps({
        "catalog": {
            "meta": {"routes": {}, "uct_validity": {}, "rail_stops": [], "extra_holidays": []},
            "days": {"weekday": {"lines": []}, "saturday": {"lines": []}, "sunday": {"lines": []}},
        },
        "cases": cases,
    }), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(RUNNER), str(path)],
        text=True, capture_output=True, cwd=REPO, check=False, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    by_id = {row["id"]: row for row in json.loads(proc.stdout)}
    day = date(2024, 1, 1)
    while day <= date(2030, 12, 31):
        row = by_id[day.isoformat()]
        assert row["day_type"] == day_type_for(day)
        assert row["holiday"] == holiday_name(day)
        day += timedelta(days=1)
    from fastapi_app.services.service_day import easter_sunday  # pylint: disable=import-outside-toplevel
    for year in range(2020, 2036):
        assert by_id[f"easter-{year}"]["easter"] == easter_sunday(year).isoformat()


def test_leg_token_matches_python():
    legs = [{
        "route_id": "T01",
        "direction": "To Civic Centre",
        "board": "Wood",
        "alight": "Civic Centre",
        "dep": "06:10:00",
        "arr": "06:40:00",
    }]
    path = REPO / "dist" / "legs-case.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "catalog": {
            "meta": {"routes": {}, "extra_holidays": []},
            "days": {"weekday": {"lines": []}, "saturday": {"lines": []}, "sunday": {"lines": []}},
        },
        "cases": [{"id": "legs", "kind": "legs", "legs": legs}],
    }), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(RUNNER), str(path)],
        text=True, capture_output=True, cwd=REPO, check=True, timeout=30,
    )
    assert json.loads(proc.stdout)[0]["token"] == encode_legs(legs)


def test_direct_myciti_all_day_types(built, con):
    for day in ("weekday", "saturday", "sunday"):
        _compare_search(
            con, built, now="2026-11-09T12:00:00+02:00",
            from_stop="Camps Bay", to_stop="Civic Centre",
            day_type=day, operator="both", when="depart", at="00:00",
        )


def test_direct_filtered_by_operator_and_time(built, con):
    _compare_search(
        con, built, now="2026-11-10T09:15:00+02:00",
        from_stop="Camps Bay", to_stop="Civic Centre",
        day_type="weekday", operator="myciti", when="depart", at="09:00",
    )
    _compare_search(
        con, built, now="2026-11-10T09:15:00+02:00",
        from_stop="Camps Bay", to_stop="Civic Centre",
        day_type="weekday", operator="metrorail", when="depart", at="00:00",
    )
    _compare_search(
        con, built, now="2026-11-10T09:15:00+02:00",
        from_stop="Camps Bay", to_stop="Civic Centre",
        day_type="weekday", operator="both", when="arrive", at="08:30",
    )


def test_metrorail_direct_and_sunday_gap(built, con):
    _compare_search(
        con, built, now="2026-11-10T07:00:00+02:00",
        from_stop="Cape Town Station", to_stop="Mowbray Station",
        day_type="weekday", operator="metrorail", when="depart", at="05:00",
    )
    _compare_search(
        con, built, now="2026-11-08T12:00:00+02:00",
        from_stop="Cape Town Station", to_stop="Bellville Station",
        day_type="sunday", operator="metrorail", when="depart", at="00:00",
    )
    _compare_search(
        con, built, now="2026-11-10T12:00:00+02:00",
        from_stop="Cape Town Station", to_stop="Mowbray Station",
        day_type="saturday", operator="metrorail", when="depart", at="00:00",
    )


def test_uct_one_transfer_and_cross_network(built, con):
    _compare_search(
        con, built, now="2026-11-10T06:00:00+02:00",
        from_stop="Claremont", to_stop="Hiddingh",
        day_type="weekday", operator="uct", when="depart", at="00:00",
    )
    _compare_search(
        con, built, now="2026-11-10T06:00:00+02:00",
        from_stop="Atlantis Station", to_stop="Hiddingh",
        day_type="weekday", operator="both", when="depart", at="06:00",
    )


def test_two_transfer_and_no_service(built, con):
    _compare_search(
        con, built, now="2026-11-10T05:00:00+02:00",
        from_stop="Big Bay", to_stop="Gardens",
        day_type="weekday", operator="both", when="depart", at="05:00",
    )
    _compare_search(
        con, built, now="2026-11-10T12:00:00+02:00",
        from_stop="Camps Bay", to_stop="Hiddingh",
        day_type="weekday", operator="both", when="depart", at="00:00",
    )


def test_live_now_next_day_and_holiday(built, con):
    _compare_search(
        con, built, now="2026-11-13T23:50:00+02:00",
        from_stop="Camps Bay", to_stop="Civic Centre",
        day_type="weekday", operator="myciti", when="now",
    )
    _compare_search(
        con, built, now="2026-12-16T11:00:00+02:00",
        from_stop="Camps Bay", to_stop="Civic Centre",
        operator="myciti", when="now", search_date="2026-12-16",
    )
    _compare_search(
        con, built, now="2026-11-09T00:20:00+02:00",
        from_stop="Civic Centre", to_stop="Camps Bay",
        day_type="weekday", operator="myciti", when="now",
    )


def test_stop_boards(built, con):
    from fastapi_app.routes.stop import _load_board, _merge_boards  # pylint: disable=import-outside-toplevel

    samples = [
        ("Civic Centre", "weekday", "both", "2026-11-10T12:00:00+02:00"),
        ("Civic Centre", "saturday", "myciti", "2026-11-14T08:00:00+02:00"),
        ("Civic Centre", "sunday", "both", "2026-11-08T15:00:00+02:00"),
        ("Claremont", "weekday", "uct", "2026-11-10T07:30:00+02:00"),
        ("Cape Town Station", "weekday", "metrorail", "2026-11-10T06:00:00+02:00"),
        ("Cape Town Station", "sunday", "metrorail", "2026-11-08T12:00:00+02:00"),
        ("Civic Centre", "weekday", "both", "2026-11-09T00:20:00+02:00"),
        ("Not A Real Stop", "weekday", "both", "2026-11-10T12:00:00+02:00"),
    ]
    cases = []
    expected = {}
    for stop, day, operator, now_s in samples:
        now = datetime.fromisoformat(now_s)
        case_id = f"{stop}|{day}|{operator}|{now_s}"
        cases.append({
            "id": case_id, "kind": "stop", "stop": stop, "day_type": day,
            "operator": operator, "now": now_s,
        })
        if operator in ("uct", "both"):
            expired, _warning = uct_expiry_state(con, now.date())
        else:
            expired = set()
        live = is_live_board(day, now)
        board = _load_board(
            con, stop, day, now.strftime("%H:%M:%S") if live else "00:00:00", operator, expired,
        )
        if live:
            carry = overnight_carryover(now)
            if carry:
                earlier = _load_board(con, stop, carry[0], carry[1], operator, expired)
                board = _merge_boards(earlier, board)
        next_day = None
        board_day = day
        if board[0] and live and not board[1]:
            tomorrow, tomorrow_type = next_service_day(now)
            later = _load_board(con, stop, tomorrow_type, "00:00:00", operator, expired)
            if later[1]:
                board = later
                board_day = tomorrow_type
                next_day = {"day_type": tomorrow_type, "label": DAY_LABELS[tomorrow_type],
                            "holiday": holiday_name(tomorrow)}
        deps = {
            key: [item["time"] for item in data["upcoming"]]
            for key, data in board[1].items()
        }
        expected[case_id] = {
            "routes": [(r["route_id"], r["direction"]) for r in board[0]],
            "departures": deps,
            "error": None if board[0] else f"No routes found for stop '{stop}' on {day}s",
            "board_day_type": board_day if board[0] else None,
            "next_day": next_day,
            "live": live,
        }
    got = {row["id"]: row["result"] for row in _node(built, cases)}
    for case_id, want in expected.items():
        result = got[case_id]
        assert [(r["route_id"], r["direction"]) for r in result["routes"]] == want["routes"]
        got_deps = {
            key: [t[:5] for t in data["times"]]
            for key, data in result["departures"].items()
        }
        assert got_deps == want["departures"]
        assert result["error"] == want["error"]
        assert result.get("next_day") == want["next_day"]
        if want["board_day_type"]:
            assert result["board_day_type"] == want["board_day_type"]
        assert result["live"] == want["live"]


def test_stop_sequence_matches_first_trip_order(built, con):
    direction = con.execute(
        """
        SELECT direction FROM departures
        WHERE route_id = '106' AND day_type = 'weekday'
              AND stop_name IN ('Camps Bay', 'Civic Centre')
        GROUP BY direction
        HAVING COUNT(DISTINCT stop_name) = 2
        ORDER BY direction
        """
    ).fetchall()
    direction = next(
        row[0] for row in direction
        if leg_stop_sequence(con, "106", row[0], "Camps Bay", "Civic Centre", "weekday")
    )
    want = leg_stop_sequence(con, "106", direction, "Camps Bay", "Civic Centre", "weekday")
    got = _node(built, [{
        "id": "seq", "kind": "sequence", "day_type": "weekday", "route_id": "106",
        "direction": direction, "board": "Camps Bay", "alight": "Civic Centre",
    }])[0]["stops"]
    assert got == want
    assert len(got) >= 2
