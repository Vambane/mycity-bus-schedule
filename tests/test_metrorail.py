"""
test_metrorail.py — Metrorail Western Cape integration
========================================================
Covers the loader (etl/scrape_metrorail.py) on a small hand-made export,
the journey engine on the loaded rows, the map/location helpers, the
Sunday and Saturday notes, and the network filter in the web app.

The fixture mirrors the Next Train export shape: each sheet is a list
whose first row is "Last Updated: …" and whose other rows are one station
each, keyed by train number, with optional COORDINATES.
"""
from datetime import date

import duckdb
import pytest

import etl.scrape_metrorail as mr
from etl.load_db import load
from fastapi_app.services.metrorail_notes import badge_label, metrorail_notice, serves_rail
from journey import find_connections, find_transfer_connections
from system_map import _route_category, _route_color


def _row(station, coords=None, **times):
    row = {"STATION": station, **times}
    if coords:
        row["COORDINATES"] = coords
    return row


# Southern Line: train 0101 calls everywhere; 0103 is an express that skips
# Woodstock (so stop counts differ, the case the old pairing got wrong);
# 0105 is a short working to Mowbray.
SOUTHERN = [
    {"STATION": "Last Updated: 2 February 2026"},
    _row("CAPE TOWN", "-33.9236, 18.4272", **{"0101": "6:00", "0103": "6:10", "0105": "6:20"}),
    _row("WOODSTOCK", "-33.9265, 18.4466", **{"0101": "6:03", "0105": "6:23"}),
    _row("SALT RIVER", "-33.9270, 18.4630", **{"0101": "6:06", "0103": "6:14", "0105": "6:26"}),
    _row("MOWBRAY", "-33.9480, 18.4720", **{"0101": "6:12", "0103": "6:19", "0105": "6:31"}),
    _row("CLAREMONT", "-33.9817, 18.4669", **{"0101": "6:20", "0103": "6:26"}),
]
# Northern Line from Salt River: shares Salt River for a transfer.
NORTHERN = [
    {"STATION": "Last Updated: 8 June 2026"},
    _row("CAPE TOWN", "-33.9236, 18.4272", **{"3201": "6:05"}),
    _row("SALT RIVER", "-33.9270, 18.4630", **{"3201": "6:12", "3203": "6:30"}),
    _row("MUTUAL", "-33.9330, 18.5100", **{"3201": "6:20", "3203": "6:38"}),
    _row("BELLVILLE", "-33.9050, 18.6290", **{"3201": "6:45", "3203": "7:03"}),
]
WC = {
    "ct_to_simon_weekday": SOUTHERN,
    "ct_to_simon_weekday_meta": "2 February 2026",
    "ct_to_strnd_weekday": NORTHERN,
    "ct_to_strnd_weekday_meta": "8 June 2026",
    "ct_to_simon_sat": [
        {"STATION": "Last Updated: 31 October 2025"},
        _row("CAPE TOWN", **{"0141": "8:00"}),
        _row("CLAREMONT", **{"0141": "8:20"}),
    ],
    "ct_to_simon_sat_meta": "31 October 2025",
    # Holiday sheets are not loaded (see module docstring).
    "public_holidays": {"ct_to_simon_pub": [_row("CAPE TOWN", **{"0101": "9:00"})]},
    "lastUpdated": "9/23/2026",
}


@pytest.fixture(name="data")
def _data(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "STATIONS_CSV", tmp_path / "metrorail_stations.csv")
    return mr.scrape_all(wc=WC)


@pytest.fixture(name="db")
def _db(data, tmp_path):
    path = tmp_path / "t.duckdb"
    load(data, path, operator="metrorail")
    return duckdb.connect(str(path))


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class TestParsers:
    def test_station_names_get_suffix_and_case(self):
        assert mr.station_name("SIMON'S TOWN") == "Simon's Town Station"
        assert mr.station_name("KOEBERG RD") == "Koeberg Rd Station"
        assert mr.station_name("") == ""

    def test_times(self):
        assert mr.norm_time("5:15") == "05:15:00"
        assert mr.norm_time("7:02:00 ") == "07:02:00"
        assert mr.norm_time("EXPRESS") is None
        assert mr.norm_time("Via MV") is None

    def test_effective_dates(self):
        assert mr.parse_effective("8 June 2026") == date(2026, 6, 8)
        assert mr.parse_effective("Last Updated: 2 February 2026") == date(2026, 2, 2)
        assert mr.parse_effective("Unspecified") is None

    @pytest.mark.parametrize("sheet,line", [
        ("ct_to_simon_weekday", "MR-S"),
        ("rtret_to_ct_sat", "MR-CF"),
        ("bellv_to_mutul_weekday", "MR-C"),
        ("ct_to_strnd_weekday", "MR-N"),
        ("malm_to_ct_weekday", "MR-M"),
        ("ct_to_nowhere_weekday", None),
    ])
    def test_sheet_lines(self, sheet, line):
        assert mr.line_for_sheet(sheet) == line


class TestCleanStopTimes:
    def test_same_minute_gets_one_second(self):
        out = mr.clean_stop_times(("weekday", "1"), [
            ("06:00:00", "A"), ("06:05:00", "B"), ("06:05:00", "C"), ("06:05:00", "D"),
        ])
        assert [t for t, _ in out] == ["06:00:00", "06:05:00", "06:05:01", "06:05:02"]

    def test_one_typo_drops_one_station(self):
        # Stikland mistyped as 09:43 between Brackenfell 09:35 and Bellville 09:36.
        out = mr.clean_stop_times(("saturday", "2504"), [
            ("09:30:00", "Eikenfontein"), ("09:35:00", "Brackenfell"),
            ("09:43:00", "Stikland"), ("09:36:00", "Bellville"),
            ("09:41:00", "Tygerberg"),
        ])
        assert [n for _, n in out] == ["Eikenfontein", "Brackenfell", "Bellville", "Tygerberg"]


class TestTrainMerging:
    def test_train_split_across_sheets_is_merged(self):
        wc = {
            "kraai_to_ct_weekday": [
                _row("KRAAIFONTEIN", **{"2500": "6:00"}), _row("BELLVILLE", **{"2500": "6:20"}),
            ],
            "bellv_to_ct_weekday": [
                _row("BELLVILLE", **{"2500": "6:21"}), _row("CAPE TOWN", **{"2500": "6:50"}),
            ],
        }
        trains, _, conflicts, _ = mr.build_trains(wc)
        stops = [n for _, n in trains[("weekday", "2500")]["stops"]]
        assert stops == ["Kraaifontein Station", "Bellville Station", "Cape Town Station"]
        assert len(conflicts) == 1  # Bellville 06:20 vs 06:21, logged not fatal

    def test_reused_train_number_stays_two_trains(self):
        # PRASA prints 3515 for two different trains on two sheets.
        wc = {
            "ct_to_well_weekday": [
                _row("CAPE TOWN", **{"3515": "16:50"}), _row("MUTUAL", **{"3515": "17:05"}),
                _row("BELLVILLE", **{"3515": "17:31"}), _row("WELLINGTON", **{"3515": "19:16"}),
            ],
            "ct_to_bellv_weekday": [
                _row("CAPE TOWN", **{"3515": "18:10"}), _row("MONTE VISTA", **{"3515": "18:45"}),
                _row("BELLVILLE", **{"3515": "19:05"}),
            ],
        }
        trains, _, _, _ = mr.build_trains(wc)
        assert ("weekday", "3515") in trains and ("weekday", "3515/2") in trains
        assert len(trains[("weekday", "3515")]["stops"]) == 4
        assert [n for _, n in trains[("weekday", "3515/2")]["stops"]][1] == "Monte Vista Station"


class TestPatternLabels:
    def test_labels_are_unique_and_readable(self):
        full = ("Cape Town Station", "Woodstock Station", "Salt River Station", "Bellville Station")
        skip = ("Cape Town Station", "Salt River Station", "Bellville Station")
        via = ("Cape Town Station", "Monte Vista Station", "Bellville Station")
        labels = mr.label_patterns({("MR-N", full): 5, ("MR-N", skip): 2, ("MR-N", via): 1})
        assert labels[("MR-N", full)] == "Cape Town to Bellville"
        assert labels[("MR-N", skip)] == "Cape Town to Bellville (skips Woodstock)"
        assert labels[("MR-N", via)] == "Cape Town to Bellville via Monte Vista"
        assert len(set(labels.values())) == 3


# ---------------------------------------------------------------------------
# Loaded rows
# ---------------------------------------------------------------------------

class TestScrapeAll:
    def test_rows_and_operator(self, data):
        assert {r["route_id"] for r in data["routes"]} == {"MR-S", "MR-N"}
        assert all(d["operator"] == "metrorail" for d in data["departures"])
        assert all(d["trip_id"] for d in data["departures"])

    def test_no_holiday_or_sunday_rows(self, data):
        assert {d["day_type"] for d in data["departures"]} == {"weekday", "saturday"}

    def test_every_pattern_has_one_stop_order(self, data):
        seqs: dict = {}
        for d in data["departures"]:
            key = (d["direction"], d["day_type"], d["trip_id"])
            seqs.setdefault(key, []).append(d["stop_name"])
        by_dir: dict = {}
        for (direction, _, _), stops in seqs.items():
            by_dir.setdefault(direction, set()).add(tuple(stops))
        assert all(len(v) == 1 for v in by_dir.values()), by_dir

    def test_validity_dates(self, data):
        tt = {(t["route_id"], t["day_type"]): t["valid_from"] for t in data["timetables"]}
        assert tt[("MR-S", "weekday")] == date(2026, 2, 2)
        assert tt[("MR-S", "saturday")] == date(2025, 10, 31)

    def test_station_coordinates_written(self, data):
        assert data["departures"]  # scrape_all wrote the CSV as a side effect
        text = mr.STATIONS_CSV.read_text()
        assert "Cape Town Station,-33.9236,18.4272" in text


class TestJourneys:
    def test_express_pairs_exactly(self, db):
        """Unequal stop counts used to mis-pair; patterns make it exact."""
        df = find_connections(db, "Cape Town Station", "Claremont Station", "weekday", "metrorail")
        assert list(zip(df["dep"], df["arr"])) == [
            ("06:00:00", "06:20:00"), ("06:10:00", "06:26:00"),
        ]

    def test_short_hop_is_kept(self, db):
        df = find_connections(db, "Woodstock Station", "Salt River Station", "weekday", "metrorail")
        assert set(df["duration_min"]) == {3}

    def test_short_working_not_offered_beyond_terminus(self, db):
        df = find_connections(db, "Woodstock Station", "Claremont Station", "weekday", "metrorail")
        assert list(df["dep"]) == ["06:03:00"]  # 0105 ends at Mowbray

    def test_transfer_between_lines(self, db):
        df = find_transfer_connections(db, "Woodstock Station", "Bellville Station",
                                       "weekday", operator="metrorail")
        assert not df.empty
        first = df.iloc[0]
        assert first["via"] == ["Salt River Station"]
        assert list(first["route_ids"]) == ["MR-S", "MR-N"]

    def test_no_sunday_trains(self, db):
        assert find_connections(db, "Cape Town Station", "Claremont Station", "sunday").empty


# ---------------------------------------------------------------------------
# Map, labels, notes
# ---------------------------------------------------------------------------

class TestMapHelpers:
    def test_rail_category_and_fixed_colour(self):
        assert _route_category("MR-S") == "Metrorail"
        assert _route_color("MR-S", 0, 0, 0) == _route_color("MR-S", 5, 5, 5)

    def test_badge_label(self):
        assert badge_label("MR-CF") == "Cape Flats"
        assert badge_label("T01") == "T01"


class TestNotes:
    def test_sunday_note(self, db):
        note = metrorail_notice(db, "sunday")
        assert note and "Sundays" in note["title"]

    def test_saturday_note_names_old_sheets(self, db):
        note = metrorail_notice(db, "saturday", today=date(2026, 11, 8))
        assert note and "Southern Line (31 October 2025)" in note["body"]

    def test_fresh_saturday_has_no_note(self, db):
        assert metrorail_notice(db, "saturday", today=date(2025, 11, 1)) is None

    def test_weekday_has_no_note(self, db):
        assert metrorail_notice(db, "weekday") is None

    def test_serves_rail(self, db):
        assert serves_rail(db, ["Gardens", "Mowbray Station"])
        assert not serves_rail(db, ["Gardens", None])


# ---------------------------------------------------------------------------
# Web app (uses the real snapshot DB)
# ---------------------------------------------------------------------------

@pytest.fixture(name="client", scope="module")
def _client():
    from fastapi.testclient import TestClient  # pylint: disable=import-outside-toplevel

    from fastapi_app.main import app  # pylint: disable=import-outside-toplevel
    with TestClient(app) as c:
        yield c


class TestWebApp:
    def test_network_toggle_offers_metrorail(self, client):
        html = client.get("/search").text
        assert 'value="metrorail"' in html

    def test_metrorail_filter_is_kept(self, client):
        html = client.get("/search", params={"operator": "metrorail"}).text
        tag = html[html.index('id="op-metrorail"'):]
        assert tag[:tag.index(">")].rstrip().endswith("checked")

    def test_api_accepts_metrorail(self, client):
        r = client.get("/api/map/network", params={"operator": "metrorail"})
        assert r.status_code == 200
        assert {x["category"] for x in r.json()["routes"]} == {"Metrorail"}

    def test_stations_have_locations(self, client):
        data = client.get("/api/stops/locations", params={"operator": "metrorail"}).json()
        assert data["stops"] and data["unmapped"] == 0

    def test_sunday_search_explains_missing_trains(self, client):
        html = client.get("/search", params={
            "from_stop": "Mowbray Station", "to_stop": "Cape Town Station",
            "day_type": "sunday", "operator": "both",
        }).text
        assert "No Metrorail trains on Sundays" in html

    def test_bus_search_has_no_rail_note(self, client):
        html = client.get("/search", params={
            "from_stop": "Civic Centre", "to_stop": "Gardens",
            "day_type": "sunday", "operator": "both",
        }).text
        assert "No Metrorail trains" not in html
