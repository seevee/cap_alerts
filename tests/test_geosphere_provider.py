"""Tests for the GeoSphere Austria provider (issue #300).

The warning-bearing fixture is the Warn API spec's own example (Schwechat,
2023, two consecutive yellow wind warnings): Austria had no warnings anywhere
when the provider was written. The quiet fixture is a live Wien-Innere Stadt
response from 2026-10-27.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.cap_alerts.const import (
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_TRACKER_ENTITY,
    GEOSPHERE_WARNINGS_URL,
)
from custom_components.cap_alerts.icons import icon_for
from custom_components.cap_alerts.providers import ProviderError, get_provider
from custom_components.cap_alerts.providers import geosphere as _geosphere_mod
from tests.conftest import StubSession

_FIXTURES = Path(__file__).parent / "fixtures"

GeoSphereProvider = _geosphere_mod.GeoSphereProvider
parse_warnings = _geosphere_mod.parse_warnings
_compute_geosphere_id = _geosphere_mod._compute_geosphere_id

# A Schwechat point, and its request URLs as the provider builds them.
_GPS = "48.13987654,16.47123456"
_URL_DE = f"{GEOSPHERE_WARNINGS_URL}?lon=16.4712&lat=48.1399&lang=de"
_URL_EN = f"{GEOSPHERE_WARNINGS_URL}?lon=16.4712&lat=48.1399&lang=en"
_OUTSIDE = '{"type":"Error","msg":"Could not find municipal for coords."}'


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _schwechat() -> dict[str, Any]:
    return _load("geosphere_warnings_schwechat.json")


def _first(body: dict[str, Any]) -> dict[str, Any]:
    return body["properties"]["warnings"][0]["properties"]


async def _fetch(responses: dict[str, Any], **options: Any):
    session = StubSession(responses)
    alerts = await GeoSphereProvider().async_fetch(
        session, {CONF_GPS_LOC: _GPS}, options, user_agent="test"
    )
    return alerts, session


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_spec_example_maps_both_warnings():
    alerts = parse_warnings(_schwechat(), "de")
    assert [a.id for a in alerts] == [
        _compute_geosphere_id(4149),
        _compute_geosphere_id(4150),
    ]
    first = alerts[0]
    assert first.provider == "geosphere"
    assert first.event == "Windwarnung"
    assert first.severity == "Moderate"
    assert first.headline.startswith("Gelbe Windwarnung von Mo, 27.03.2023 08:00")
    # Impacts first, then the forecaster's synopsis.
    assert first.description.startswith("* Äste können herabstürzen")
    assert "Mit einer nordwestlichen Strömung" in first.description
    assert first.instruction is not None
    assert first.instruction.startswith("* Seien Sie in Wäldern")
    assert first.area_desc == "Schwechat"
    assert dict(first.geocodes) == {"GKZ": ("30740",)}
    assert first.language == "de"
    assert first.status == "Actual"
    assert first.category == "Met"
    assert first.sender == "GeoSphere Austria"


def test_times_come_from_the_epoch():
    alert = parse_warnings(_schwechat(), "de")[0]
    # 08:00 CEST is 06:00Z; the epoch says so whatever the spec's prose does.
    assert alert.onset == "2023-03-27T06:00:00+00:00"
    assert alert.effective == alert.onset
    assert alert.expires == "2023-03-27T16:00:00+00:00"
    assert alert.sent == "2023-03-27T06:00:00+00:00"


@pytest.mark.parametrize(
    ("begin", "expected"),
    [
        # The example's local form, in summer time and in winter time.
        ("27.03.2023 08:00", "2023-03-27T06:00:00+00:00"),
        ("14.01.2024 08:00", "2024-01-14T07:00:00+00:00"),
        # The schema's ISO form, offset included.
        ("2023-03-14T06:00:00+02:00", "2023-03-14T04:00:00+00:00"),
        # ISO with no offset is Vienna local, like the dotted form.
        ("2024-01-14T08:00:00", "2024-01-14T07:00:00+00:00"),
        ("not a time", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_local_strings_are_the_fallback(begin, expected):
    body = _schwechat()
    props = _first(body)
    props["rawinfo"] = {}
    props["begin"] = begin
    assert parse_warnings(body, "de")[0].onset == expected


def test_unreadable_epoch_falls_back_to_local():
    body = _schwechat()
    _first(body)["rawinfo"]["start"] = "soon"
    assert parse_warnings(body, "de")[0].onset == "2023-03-27T06:00:00+00:00"


def test_sent_without_offset_is_utc_and_garbage_is_empty():
    body = _schwechat()
    _first(body)["create"] = "2023-03-27 06:00:00"
    body["properties"]["warnings"][1]["properties"]["create"] = "yesterday"
    first, second = parse_warnings(body, "de")
    assert first.sent == "2023-03-27T06:00:00+00:00"
    assert second.sent == ""


def test_missing_create_leaves_sent_empty():
    body = _schwechat()
    del _first(body)["create"]
    assert parse_warnings(body, "de")[0].sent == ""


def test_identity_survives_a_revision():
    before = parse_warnings(_schwechat(), "de")[0]
    body = _schwechat()
    _first(body)["chgid"] = 7
    _first(body)["verlaufid"] = 3
    after = parse_warnings(body, "de")[0]
    assert after.id == before.id
    assert after.identifier != before.identifier
    assert after.parameters is not None
    assert after.parameters["chgid"] == 7


def test_msg_type_follows_the_revision_counter():
    body = _schwechat()
    _first(body)["chgid"] = 1
    first, second = parse_warnings(body, "de")
    assert first.msg_type == "Alert"
    assert second.msg_type == "Update"


def test_missing_revision_counter_is_a_first_issue():
    body = _schwechat()
    del _first(body)["chgid"]
    alert = parse_warnings(body, "de")[0]
    assert alert.msg_type == "Alert"
    assert alert.identifier == "geosphere:4149:0"


def test_parameters_drop_empty_values():
    params = parse_warnings(_schwechat(), "de")[0].parameters
    assert params == {
        "warnid": 4149,
        "chgid": 6,
        "verlaufid": 2,
        "warntypid": 1,
        "warnstufeid": 1,
    }


def test_empty_recommendations_are_none():
    body = _schwechat()
    _first(body)["empfehlungen"] = ""
    assert parse_warnings(body, "de")[0].instruction is None


@pytest.mark.parametrize(
    ("level", "severity"), [(1, "Moderate"), (2, "Severe"), (3, "Extreme"), (9, "")]
)
def test_level_ladder(level, severity):
    body = _schwechat()
    _first(body)["warnstufeid"] = level
    assert parse_warnings(body, "de")[0].severity == severity


def test_rawinfo_codes_stand_in_for_missing_top_level_ones():
    body = _schwechat()
    props = _first(body)
    del props["warntypid"], props["warnstufeid"]
    props["rawinfo"]["wtype"] = 5
    props["rawinfo"]["wlevel"] = 3
    alert = parse_warnings(body, "en")[0]
    assert alert.event == "Thunderstorm warning"
    assert alert.severity == "Extreme"


@pytest.mark.parametrize(("lang", "event"), [("de", "Warnung"), ("en", "Warning")])
def test_unknown_type_gets_a_generic_event(lang, event):
    body = _schwechat()
    _first(body)["warntypid"] = 42
    assert parse_warnings(body, lang)[0].event == event


def test_warnings_without_an_id_or_properties_are_skipped():
    body = _schwechat()
    del _first(body)["warnid"]
    body["properties"]["warnings"].append({"type": "Warning"})
    assert [a.id for a in parse_warnings(body, "de")] == [_compute_geosphere_id(4150)]


def test_non_dict_rawinfo_is_ignored():
    body = _schwechat()
    _first(body)["rawinfo"] = "1679896800"
    assert parse_warnings(body, "de")[0].onset == "2023-03-27T06:00:00+00:00"


def test_missing_location_leaves_area_empty():
    body = _schwechat()
    del body["properties"]["location"]
    alert = parse_warnings(body, "de")[0]
    assert alert.area_desc == ""
    assert not alert.geocodes


def test_icon_is_keyed_on_the_type_code():
    body = _schwechat()
    _first(body)["warntypid"] = 5
    for lang in ("de", "en"):
        assert icon_for(parse_warnings(body, lang)[0]) == "mdi:weather-lightning"


def test_unknown_type_falls_through_to_the_shared_sweep():
    body = _schwechat()
    _first(body)["warntypid"] = 42
    alert = parse_warnings(body, "en")[0]
    assert icon_for(alert) == "mdi:alert"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_geometry_is_reprojected_into_austria():
    alert = parse_warnings(_schwechat(), "de")[0]
    assert alert.geometry is not None
    # One polygon in the MultiPolygon collapses to a Polygon.
    assert alert.geometry["type"] == "Polygon"
    ring = alert.geometry["coordinates"][0]
    assert ring[0] == ring[-1]
    for lon, lat in ring:
        assert 16.4 < lon < 16.6 and 48.0 < lat < 48.2


def test_map_link_frames_the_native_bbox():
    alert = parse_warnings(_schwechat(), "en")[0]
    assert alert.web == (
        "https://warnungen.zamg.at/wsapp/en/alle/heute/630691,470596,641276,478130"
    )
    # Never the request URL, which carries the entry's coordinates.
    assert alert.url == alert.web


def test_holes_stay_holes_and_bad_polygons_drop():
    body = _schwechat()
    outer = body["geometry"]["coordinates"][0][0]
    hole = [[636000, 474000], [637000, 474000], [637000, 475000], [636000, 474000]]
    degenerate = [[1, 1], [1, 1], [1, 1], [1, 1]]
    body["geometry"]["coordinates"] = [
        [outer, hole, degenerate],
        [degenerate, hole],
        [outer],
    ]
    geometry = parse_warnings(body, "de")[0].geometry
    assert geometry is not None
    assert geometry["type"] == "MultiPolygon"
    # The degenerate hole drops, the polygon with a degenerate outer drops.
    assert [len(p) for p in geometry["coordinates"]] == [2, 1]


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        {"type": "Point", "coordinates": [636000, 474000]},
        {"type": "MultiPolygon", "coordinates": "nope"},
        {"type": "MultiPolygon", "coordinates": [[["x", "y"]]]},
        {"type": "MultiPolygon", "coordinates": []},
    ],
)
def test_unusable_geometry_ships_the_alert_without_a_shape(geometry):
    body = _schwechat()
    body["geometry"] = geometry
    alert = parse_warnings(body, "de")[0]
    assert alert.geometry is None
    assert alert.web == ""


def test_polygon_geometry_is_accepted():
    body = _schwechat()
    body["geometry"] = {
        "type": "Polygon",
        "coordinates": body["geometry"]["coordinates"][0],
    }
    geometry = parse_warnings(body, "de")[0].geometry
    assert geometry is not None and geometry["type"] == "Polygon"


# ---------------------------------------------------------------------------
# Shape errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"type": "Error", "msg": "an error occurred"},
        {"type": "error", "msg": "An error occurred"},
        [],
        {"type": "Feature"},
        {"type": "Feature", "properties": {"warnings": None}},
    ],
)
def test_bad_bodies_raise(body):
    with pytest.raises(ProviderError):
        parse_warnings(body, "de")


def test_quiet_municipality_is_empty():
    assert parse_warnings(_load("geosphere_warnings_quiet.json"), "de") == []


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def test_fetch_sends_the_rounded_point_and_language():
    alerts, session = await _fetch({_URL_DE: json.dumps(_schwechat())})
    assert session.requested == [_URL_DE]
    assert session.request_headers == [{"User-Agent": "test"}]
    assert len(alerts) == 2


async def test_fetch_asks_for_english_when_configured():
    alerts, session = await _fetch(
        {_URL_EN: json.dumps(_schwechat())}, **{CONF_LANGUAGE: "EN"}
    )
    assert session.requested == [_URL_EN]
    assert alerts[0].event == "Wind warning"
    assert alerts[0].language == "en"


async def test_unknown_language_option_falls_back_to_german():
    _alerts, session = await _fetch(
        {_URL_DE: json.dumps(_schwechat())}, **{CONF_LANGUAGE: "auto"}
    )
    assert session.requested == [_URL_DE]


async def test_quiet_fetch_is_empty():
    alerts, _session = await _fetch(
        {_URL_DE: json.dumps(_load("geosphere_warnings_quiet.json"))}
    )
    assert alerts == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ((404, _OUTSIDE), "outside Austria"),
        ((500, '{"type":"error"}'), "HTTP 500"),
        ("<html>", "malformed JSON"),
    ],
)
async def test_fetch_failures_raise_without_coordinates(response, message):
    with pytest.raises(ProviderError, match=message) as err:
        await _fetch({_URL_DE: response})
    assert "48.1" not in str(err.value)
    assert "16.4" not in str(err.value)


async def test_fetch_without_a_point_raises():
    with pytest.raises(ProviderError, match="invalid GPS"):
        await GeoSphereProvider().async_fetch(StubSession({}), {}, {})


async def test_no_user_agent_sends_no_headers():
    session = StubSession({_URL_DE: json.dumps(_schwechat())})
    await GeoSphereProvider().async_fetch(session, {CONF_GPS_LOC: _GPS}, {})
    assert session.request_headers == [None]


def test_provider_is_registered():
    provider = get_provider("geosphere")
    assert provider.name == "geosphere"
    assert "geosphere" in provider.conventions


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


async def _validate(responses: dict[str, Any], config: dict[str, Any]):
    session = StubSession(responses)
    result = await GeoSphereProvider().async_validate_config(
        session, config, user_agent="test"
    )
    return result, session


async def test_an_austrian_point_validates():
    result, session = await _validate(
        {_URL_DE: json.dumps(_load("geosphere_warnings_quiet.json"))},
        {CONF_GPS_LOC: _GPS},
    )
    assert result is None
    assert session.requested == [_URL_DE]


async def test_a_point_outside_austria_is_rejected():
    result, _session = await _validate({_URL_DE: (404, _OUTSIDE)}, {CONF_GPS_LOC: _GPS})
    assert result == "geosphere_outside_austria"


async def test_a_service_error_is_cannot_connect():
    result, _session = await _validate({_URL_DE: (503, "")}, {CONF_GPS_LOC: _GPS})
    assert result == "cannot_connect"


@pytest.mark.parametrize(
    "config", [{CONF_TRACKER_ENTITY: "device_tracker.phone"}, {CONF_GPS_LOC: "x"}]
)
async def test_nothing_to_ask_without_a_point(config):
    result, session = await _validate({}, config)
    assert result is None
    assert session.requested == []


async def test_validation_without_user_agent():
    session = StubSession({_URL_DE: json.dumps(_load("geosphere_warnings_quiet.json"))})
    assert (
        await GeoSphereProvider().async_validate_config(session, {CONF_GPS_LOC: _GPS})
        is None
    )
    assert session.request_headers == [None]
