"""The parity check, run against a stand-in for the service (#316).

``scripts/nws_zone_parity.py`` compares the alerts the local mode places at a
point with what ``alerts/active?point=`` returns. It is run by hand against
the live service, so nothing in the suite would notice if a change broke the
comparison itself: a check that always printed MATCH would pass every gate.

Here its ``get()`` is replaced by a stand-in that answers as the service
does, over a small made-up set of zones and alerts. ``point=`` is answered by
strict containment, and ``zone=`` with every alert that names a zone asked
for. The check must then report MATCH where the two agree, MATCH+ where only
the tolerance band adds an alert, and DIFFER when the zone reply is made to
lose an alert that ``point=`` returned.

No network is used. Points are public ones around New York Harbor.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from custom_components.cap_alerts.providers.nws_matching import (
    in_alert_polygon,
    request_codes,
)
from custom_components.cap_alerts.providers.nws_zones import ZoneSet

_ROOT = Path(__file__).parent.parent
_META = {"epsilon_deg": 0.005, "tail_epsilon_deg": 0.02, "tail_states": ["AK"]}
_ZONES_URL = "https://api.weather.gov/zones"

Ring = list[list[float]]


def _square(west: float, south: float, east: float, north: float) -> Ring:
    return [[west, south], [east, south], [east, north], [west, north]]


def _lattice() -> ZoneSet:
    """Half-degree squares over 38 to 45 north, 77 to 71 west, each a
    forecast zone and a county: 336 zones, more than one request carries
    when the radius is wide."""
    records = []
    for row in range(14):
        for column in range(12):
            number = f"{row * 12 + column:03d}"
            west, south = -77.0 + 0.5 * column, 38.0 + 0.5 * row
            ring = _square(west, south, west + 0.5, south + 0.5)
            box = [west, south, west + 0.5, south + 0.5]
            for layer, letter in (("forecast", "Z"), ("county", "C")):
                records.append(
                    {
                        "code": f"NY{letter}{number}",
                        "layer": layer,
                        "bbox": box,
                        "rings": [ring],
                    }
                )
    return ZoneSet.from_json(json.dumps({"meta": _META, "zones": records}))


def _zone_alert(alert_id: str, *codes: str) -> dict[str, Any]:
    urls = [
        f"{_ZONES_URL}/{'county' if code[2] == 'C' else 'forecast'}/{code}"
        for code in codes
    ]
    return {
        "geometry": None,
        "properties": {"id": alert_id, "event": "Zone Product", "affectedZones": urls},
    }


def _polygon_alert(alert_id: str, ring: Ring, county: str) -> dict[str, Any]:
    return {
        "geometry": {"type": "Polygon", "coordinates": [[*ring, ring[0]]]},
        "properties": {
            "id": alert_id,
            "event": "Polygon Warning",
            "affectedZones": [f"{_ZONES_URL}/county/{county}"],
        },
    }


class _Service:
    """Answers ``point=`` and ``zone=`` the way the service does."""

    def __init__(self, zones: ZoneSet, alerts: list[dict[str, Any]]) -> None:
        self.zones = zones
        self.alerts = alerts
        self.lost: set[str] = set()  # alert IDs the zone reply leaves out
        self.point_calls: list[str] = []
        self.zone_calls: list[list[str]] = []

    @staticmethod
    def _codes(alert: dict[str, Any]) -> set[str]:
        return {url.rsplit("/", 1)[1] for url in alert["properties"]["affectedZones"]}

    def get(self, url: str) -> dict[str, Any]:
        query = parse_qs(urlparse(url).query)
        if "point" in query:
            self.point_calls.append(query["point"][0])
            lat, lon = (float(value) for value in query["point"][0].split(","))
            inside = {hit.code for hit in self.zones.containing(lat, lon)}
            found = []
            for alert in self.alerts:
                in_polygon = in_alert_polygon(lat, lon, alert["geometry"])
                if in_polygon or (in_polygon is None and self._codes(alert) & inside):
                    found.append(alert)
            return {"features": found}
        asked = query["zone"][0].split(",")
        self.zone_calls.append(asked)
        return {
            "features": [
                alert
                for alert in self.alerts
                if self._codes(alert) & set(asked)
                and alert["properties"]["id"] not in self.lost
            ]
        }


@pytest.fixture(scope="module")
def parity() -> Any:
    spec = importlib.util.spec_from_file_location(
        "nws_zone_parity", _ROOT / "scripts" / "nws_zone_parity.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["nws_zone_parity"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def zones() -> ZoneSet:
    return _lattice()


# Inside the square NYZ065 / NYC065 (40.5 to 41.0 north, 74.5 to 74.0 west).
_POINT = (40.7033, -74.0170)
# 150 m inside the same square's western edge: NYZ064 counts through the band.
_BAND_POINT = (40.7033, -74.4982)

_ALERTS = [
    _zone_alert("own-zone", "NYZ065"),
    _zone_alert("own-county", "NYC065"),
    _zone_alert("next-door", "NYZ064"),
    _zone_alert("far", "NYZ000"),
    _polygon_alert("around", _square(-74.1, 40.65, -73.95, 40.75), "NYC065"),
    _polygon_alert("beside", _square(-74.20, 40.65, -74.08, 40.75), "NYC065"),
]


def _run(parity, monkeypatch, capsys, service: _Service, points) -> tuple[int, str]:
    monkeypatch.setattr(parity, "get", service.get)
    monkeypatch.setattr(parity.time, "sleep", lambda _seconds: None)
    failing = parity.check(service.zones, list(points))
    return failing, capsys.readouterr().out


def test_where_the_two_agree_the_check_reports_a_match(
    parity, monkeypatch, capsys, zones
):
    service = _Service(zones, _ALERTS)
    failing, out = _run(parity, monkeypatch, capsys, service, [_POINT])
    assert failing == 0
    # How many alerts the zone reply holds depends on where the centre fell.
    assert "point= 3   zone= " in out
    assert "here -> 3 (strict 3)" in out
    # Nearby: the polygon 5 km to the west. The zone next door is 40 km off.
    assert "nearby within 16 km -> 1   MATCH\n" in out
    assert "1 of 1 points match" in out


def test_the_request_is_made_away_from_the_point_and_names_every_zone_in_range(
    parity, monkeypatch, capsys, zones
):
    """The zone list is not the list for the point: it is calculated around a
    centre 24 to 56 km away and holds every zone the point needs."""
    service = _Service(zones, _ALERTS)
    _failing, out = _run(parity, monkeypatch, capsys, service, [_POINT])
    assert service.point_calls == [f"{_POINT[0]},{_POINT[1]}"]
    (asked,) = service.zone_calls
    assert asked == sorted(set(asked))
    needed = {hit.code for hit in zones.resolve(*_POINT, radius_km=16.0, max_zones=999)}
    assert needed <= set(asked)
    assert asked != request_codes(zones, *_POINT, 16.0)
    away = float(out.split("from a centre ")[1].split(" km away")[0])
    assert 24.0 <= away <= 57.0
    assert f"asked about {len(asked)} zones in 1 request(s)" in out


def test_an_alert_only_the_band_admits_is_a_match_with_extras(
    parity, monkeypatch, capsys, zones
):
    service = _Service(zones, _ALERTS)
    failing, out = _run(parity, monkeypatch, capsys, service, [_BAND_POINT])
    assert failing == 0
    assert "MATCH+\n" in out
    assert "extra, from the band   : Zone Product  (next-door)" in out
    assert "(strict 2)" in out


def test_an_alert_point_returns_and_the_zone_reply_loses_is_reported_and_fails(
    parity, monkeypatch, capsys, zones
):
    """The condition the script exists to find. Without this the comparison
    could be broken and every run would still print MATCH."""
    service = _Service(zones, _ALERTS)
    service.lost = {"own-zone"}
    failing, out = _run(parity, monkeypatch, capsys, service, [_POINT, _BAND_POINT])
    assert failing == 2
    assert out.count("DIFFER\n") == 2
    assert "own-zone" in out
    assert "0 of 2 points match" in out


def test_an_alert_placed_here_that_point_does_not_return_fails(
    parity, monkeypatch, capsys, zones
):
    """The other direction: something extra that the band does not explain."""
    service = _Service(zones, _ALERTS)
    real_get = service.get

    def without_the_polygon_alert(url: str) -> dict[str, Any]:
        reply = real_get(url)
        if "point=" in url:
            reply["features"] = [
                alert
                for alert in reply["features"]
                if alert["properties"]["id"] != "around"
            ]
        return reply

    monkeypatch.setattr(parity, "get", without_the_polygon_alert)
    monkeypatch.setattr(parity.time, "sleep", lambda _seconds: None)
    failing = parity.check(zones, [_POINT])
    out = capsys.readouterr().out
    assert failing == 1
    assert "DIFFER\n" in out
    assert "around" in out


def test_a_long_list_is_sent_in_parts_and_an_alert_in_two_parts_counts_once(
    parity, monkeypatch, capsys, zones
):
    """At 400 km every one of the 336 codes is asked about, in more than one
    request. The alert naming the first code and the last comes back in two
    replies and must be counted once."""
    monkeypatch.setattr(parity, "RADIUS_KM", 400.0)
    spanning = _zone_alert("spanning", "NYC000", "NYZ167")
    service = _Service(zones, [*_ALERTS, spanning])
    failing, out = _run(parity, monkeypatch, capsys, service, [_POINT])
    assert failing == 0
    assert len(service.zone_calls) >= 2
    assert all(len(part) <= 300 for part in service.zone_calls)
    asked = [code for part in service.zone_calls for code in part]
    assert len(asked) == len(set(asked)) == 336
    assert sum("NYC000" in part for part in service.zone_calls) == 1
    assert sum("NYZ167" in part for part in service.zone_calls) == 1
    assert not any({"NYC000", "NYZ167"} <= set(part) for part in service.zone_calls)
    assert f"in {len(service.zone_calls)} request(s)" in out
    assert "zone= 7 " in out  # seven different alerts, the spanning one once
