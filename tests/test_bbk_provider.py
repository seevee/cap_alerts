"""Tests for the BBK / NINA provider — CAP-over-JSON two-step fetch (issue #66).

Fixtures are trimmed captures from warnung.bund.de on 2026-09-19: a DWD
gust warning as the ``dwd`` mapData index lists it (``dwdmap.`` id), the
same warning class as a district dashboard lists it (``dwd.`` id, many
areas), and a MoWaS drinking-water notice with ``de`` / ``de-LS`` / ``en``
blocks and no ``expires``. The MoWaS all-clear (``Cancel``, with an
``expires``) is a capture of 2026-09-20.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.cap_alerts.const import (
    BBK_CHANNELS,
    BBK_DASHBOARD_URL,
    BBK_GEOJSON_URL,
    BBK_MAPDATA_URL,
    BBK_WARNING_URL,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_ZONE_ID,
)
from custom_components.cap_alerts.normalize import _bbox_from_geometry
from custom_components.cap_alerts.providers import bbk as _bbk_mod
from custom_components.cap_alerts.providers import cap as _cap_mod
from custom_components.cap_alerts.providers.cap_content_cache import CAPContentCache
from tests.conftest import StubSession

_FIXTURES = Path(__file__).parent / "fixtures"

BBKProvider = _bbk_mod.BBKProvider
normalize_ars = _bbk_mod.normalize_ars
channel_for = _bbk_mod.channel_for
compute_bbk_id = _bbk_mod.compute_bbk_id
_parse_index = _bbk_mod._parse_index
_live_entries = _bbk_mod._live_entries
_IndexEntry = _bbk_mod._IndexEntry
_rings_from_geojson = _bbk_mod._rings_from_geojson
cap_doc_from_json = _cap_mod.cap_doc_from_json

_ARS = "110000000000"
_DWD_MAP_ID = (
    "dwdmap.2.49.0.0.276.0.DWD.PVW.1789822740000."
    "0e032817-18f7-4426-ba05-2d596cd4616b.MUL"
)
_DWD_MAP_ID_2 = (
    "dwdmap.2.49.0.0.276.0.DWD.PVW.1789834380000."
    "9d13fcc3-432d-43b1-894b-4a2a3740d582.MUL"
)
_DWD_DASH_ID = (
    "dwd.2.49.0.0.276.0.DWD.PVW.1789837260000.f831dd90-b825-45e6-b453-68d2579831c5.MUL"
)
_MOW_ID = "mow.DE-SL-SLS-W038-20260904-000"
_MOW_ID_2 = "mow.DE-BW-LB-W026-20260916-001"
_MOW_ID_3 = "mow.DE-SH-FL-SE100-20260919-100-000"
# A MoWaS all-clear and the warning it retires (capture of 2026-09-20).
_MOW_CANCEL_ID = "mow.DE-SL-SB-SE035-20260920-35-001"
_MOW_CANCELLED_ID = "mow.DE-SL-SB-SE035-20260920-35-000"

# A point inside each fixture polygon, and one outside both.
_IN_DWD_MAP = "51.82,10.70"
_IN_MOWAS = "49.37,6.60"
_IN_SIERSBURG = "49.36,6.674"
_IN_NEITHER = "48.14,11.58"

# Every fixture expiry is in September 2026; this clock keeps them all live.
_NOW = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _warning_url(warning_id: str) -> str:
    return BBK_WARNING_URL.format(warning_id=warning_id)


def _geojson_url(warning_id: str) -> str:
    return BBK_GEOJSON_URL.format(warning_id=warning_id)


def _mapdata_url(channel: str) -> str:
    return BBK_MAPDATA_URL.format(channel=channel)


def _region_responses() -> dict[str, Any]:
    return {
        BBK_DASHBOARD_URL.format(ars=_ARS): _fixture("bbk_dashboard.json"),
        _warning_url(_DWD_DASH_ID): _fixture("bbk_warning_dwd_dashboard.json"),
        _geojson_url(_DWD_DASH_ID): _fixture("bbk_geojson_dwd_dashboard.geojson"),
    }


def _gps_responses() -> dict[str, Any]:
    """The five channel indexes, with documents for one DWD and one MoWaS entry.

    The other MoWaS entries have no document behind them (404), and the second
    DWD entry is left live so the expiry filter is exercised separately.
    """
    responses: dict[str, Any] = {
        _mapdata_url(c): "[]" for c in BBK_CHANNELS if c not in ("dwd", "mowas")
    }
    responses[_mapdata_url("dwd")] = _fixture("bbk_mapdata_dwd.json")
    responses[_mapdata_url("mowas")] = _fixture("bbk_mapdata_mowas.json")
    responses[_warning_url(_DWD_MAP_ID)] = _fixture("bbk_warning_dwd.json")
    responses[_geojson_url(_DWD_MAP_ID)] = _fixture("bbk_geojson_dwd.geojson")
    responses[_warning_url(_MOW_ID)] = _fixture("bbk_warning_mowas.json")
    responses[_geojson_url(_MOW_ID)] = _fixture("bbk_geojson_mowas.geojson")
    return responses


async def _fetch(
    responses: dict[str, Any],
    config: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> tuple[list, StubSession]:
    session = StubSession(responses)
    alerts = await BBKProvider().async_fetch(
        session,  # type: ignore[arg-type]
        config,
        options or {},
        cap_content_cache=CAPContentCache(),
        user_agent="test",
    )
    return alerts, session


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    """Hold the expiry filter's clock inside the fixtures' validity window."""
    real = _bbk_mod._live_entries

    def _pinned(entries, now=None):
        return real(entries, now or _NOW)

    monkeypatch.setattr(_bbk_mod, "_live_entries", _pinned)


# ---------------------------------------------------------------------------
# Regionalschlüssel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("095640000000", "095640000000"),
        ("09564", "095640000000"),
        (" 09564 ", "095640000000"),
        # A municipality widens to its district: the dashboard answers only there.
        ("010510011011", "010510000000"),
        ("0956412", "095640000000"),
    ],
)
def test_normalize_ars_widens_to_the_district(raw: str, expected: str):
    assert normalize_ars(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "0956", "0956400000001", "0956A", "DE-BY"])
def test_normalize_ars_rejects_bad_input(raw: str):
    assert normalize_ars(raw) is None


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------


def test_parse_dashboard_reads_id_and_expires():
    entries = _parse_index(
        json.loads(_fixture("bbk_dashboard.json")), expires_key="expires"
    )
    assert entries == [_IndexEntry(_DWD_DASH_ID, "2026-09-20T20:00:00+02:00")]


def test_parse_mapdata_reads_expires_date_where_published():
    dwd = _parse_index(
        json.loads(_fixture("bbk_mapdata_dwd.json")), expires_key="expiresDate"
    )
    assert [e.warning_id for e in dwd] == [_DWD_MAP_ID, _DWD_MAP_ID_2]
    assert dwd[0].expires == "2026-09-21T08:00:00+02:00"
    mowas = _parse_index(
        json.loads(_fixture("bbk_mapdata_mowas.json")), expires_key="expiresDate"
    )
    # The civil-protection channels publish no expiry on the index at all.
    assert [e.expires for e in mowas] == ["", "", ""]


def test_parse_index_skips_rows_without_an_id_and_non_lists():
    payload = [{"id": " x.1 "}, {"expires": "later"}, "junk", {"id": 5}]
    assert _parse_index(payload, expires_key="expires") == [_IndexEntry("x.1", "")]
    assert _parse_index({"id": "x.1"}, expires_key="expires") == []
    assert _parse_index(None, expires_key="expires") == []


def test_live_entries_drops_expired_and_duplicates_but_keeps_the_unparseable():
    entries = [
        _IndexEntry("a", "2026-09-19T21:00:00+02:00"),  # 19:00Z, past
        _IndexEntry("b", "2026-09-19T23:00:00+02:00"),  # 21:00Z, live
        _IndexEntry("b", ""),  # the mapData union can list one id twice
        _IndexEntry("c", ""),  # no expiry: never dropped here
        _IndexEntry("d", "not a date"),  # fail open
        _IndexEntry("e", "2026-09-19T20:00:00"),  # naive → UTC, exactly now
    ]
    assert [e.warning_id for e in _live_entries(entries, _NOW)] == ["b", "c", "d"]


# ---------------------------------------------------------------------------
# Identity and channel
# ---------------------------------------------------------------------------


def test_channel_from_id_prefix():
    assert channel_for(_DWD_MAP_ID) == "dwd"
    assert channel_for(_DWD_DASH_ID) == "dwd"
    assert channel_for(_MOW_ID) == "mowas"
    assert channel_for("kat.something") == "katwarn"
    assert channel_for("biw.something") == "biwapp"
    assert channel_for("lhp.something") == "lhp"
    assert channel_for("police.x") == ""
    assert channel_for("noprefix") == ""


def test_id_is_a_stable_hash_of_the_identifier():
    assert compute_bbk_id(_MOW_ID) == compute_bbk_id(_MOW_ID)
    assert len(compute_bbk_id(_MOW_ID)) == 12
    assert compute_bbk_id(_MOW_ID) != compute_bbk_id(_MOW_ID_2)


# ---------------------------------------------------------------------------
# CAP-over-JSON reader (lives in cap.py, exercised on BBK documents)
# ---------------------------------------------------------------------------


def test_json_reader_yields_the_same_doc_shape_as_xml():
    doc = cap_doc_from_json(json.loads(_fixture("bbk_warning_dwd.json")))
    assert doc is not None
    assert doc.identifier == _DWD_MAP_ID
    assert doc.sender == "opendata@dwd.de"
    assert doc.msg_type == "Update"
    assert doc.status == "Actual"
    assert doc.scope == "Public"
    assert doc.references == [
        (
            "opendata@dwd.de",
            (
                "dwdmap.2.49.0.0.276.0.DWD.PVW.1789750500000."
                "41038775-570c-4a6b-8000-c33f7cad7e54.MUL"
            ),
            "2026-09-18T16:55:00-00:00",
        )
    ]
    assert [i.language for i in doc.infos] == ["de-DE", "en", "fr"]
    de = doc.infos[0]
    assert de.category == "Met"
    assert de.event == "SCHWERE STURMBÖEN"
    assert de.response_type == ["Prepare"]
    assert de.severity == "Moderate"
    assert de.expires == "2026-09-21T08:00:00+02:00"
    assert de.event_codes["GROUP"] == "WIND"
    assert de.event_codes["II"] == "53"
    assert de.parameters["gusts"] == "75-100 [km/h]"
    assert de.area_desc == "Stadt Wernigerode"
    assert [a.area_desc for a in de.areas] == ["Stadt Wernigerode"]
    assert de.polygons == [] and de.geocodes == {}


def test_json_reader_on_a_mowas_document():
    doc = cap_doc_from_json(json.loads(_fixture("bbk_warning_mowas.json")))
    assert doc is not None
    assert [i.language for i in doc.infos] == ["de", "de-LS", "en", "fr"]
    assert doc.references == [
        (
            "DE-SL-SLS-W038",
            "mow.DE-SL-SLS-W038-20260901-000",
            "2026-09-01T14:57:23-00:00",
        )
    ]
    de = doc.infos[0]
    assert de.category == "Health"
    assert de.event_codes == {"profile:DE-BBK-EVENTCODE": "BBK-EVC-069"}
    assert de.parameters["sender_langname"] == "Katastrophenschutzbehörde LK Saarlouis"
    assert de.expires == "" and de.onset == ""
    assert de.sender_name == ""


def test_json_reader_on_a_mowas_all_clear():
    """An "Entwarnung" is a ``Cancel`` revision with an expiry, unlike the warning."""
    doc = cap_doc_from_json(json.loads(_fixture("bbk_warning_mowas_cancel.json")))
    assert doc is not None
    assert doc.msg_type == "Cancel"
    assert doc.references == [
        ("DE-SL-SB-SE035", _MOW_CANCELLED_ID, "2026-09-20T14:02:18-00:00")
    ]
    de = doc.infos[0]
    assert de.response_type == ["AllClear"]
    assert de.expires == "2026-09-20T23:50:56+02:00"
    assert de.headline == "Entwarnung: Stromabschaltung - Siersburg"


def test_json_reader_accepts_scalar_and_list_spellings_and_shapes():
    payload = {
        "identifier": " x ",
        "references": ["a,b,c", "d,e,f", "junk"],
        "info": [
            {
                "language": "en",
                "category": "Met",
                "responseType": "Shelter",
                "eventCode": [
                    {"valueName": "N", "value": 7},
                    {"value": "no name"},
                    "junk",
                ],
                "area": [
                    {
                        "areaDesc": "Here",
                        "geocode": [
                            {"valueName": "ARS", "value": "01"},
                            {"valueName": "ARS", "value": "01"},
                        ],
                        "polygon": "1,2 3,4 5,6",
                        "circle": ["50,10 0"],
                    },
                    {"areaDesc": "There", "polygon": ["7,8 9,10 11,12", "bad"]},
                    "junk",
                ],
            },
            "junk",
        ],
    }
    doc = cap_doc_from_json(payload)
    assert doc is not None
    assert doc.identifier == "x"
    assert doc.references == [("a", "b", "c"), ("d", "e", "f")]
    (info,) = doc.infos
    assert info.category == "Met"
    assert info.response_type == ["Shelter"]
    assert info.event_codes == {"N": "7"}
    assert info.area_desc == "Here, There"
    assert info.geocodes == {"ARS": ["01"]}
    assert len(info.polygons) == 2 and len(info.areas[0].polygons) == 1
    assert info.circles == [(10.0, 50.0, 0.0)]


@pytest.mark.parametrize(
    "payload", [None, [], "x", {}, {"identifier": ""}, {"info": []}]
)
def test_json_reader_rejects_non_documents(payload):
    assert cap_doc_from_json(payload) is None


def test_json_reader_tolerates_missing_and_odd_optional_fields():
    doc = cap_doc_from_json({"identifier": "x", "references": 5, "info": "nope"})
    assert doc is not None and doc.references == [] and doc.infos == []


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_rings_from_geojson_reads_polygon_features():
    rings = _rings_from_geojson(_fixture("bbk_geojson_dwd.geojson"))
    assert len(rings) == 1
    assert rings[0][0] == [10.5738, 51.8108]
    assert len(_rings_from_geojson(_fixture("bbk_geojson_dwd_dashboard.geojson"))) == 2


def test_rings_from_geojson_accepts_multipolygon_and_skips_the_rest():
    body = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "geometry": {
                        "type": "MultiPolygon",
                        "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]], []],
                    }
                },
                {"geometry": {"type": "Point", "coordinates": [1, 2]}},
                {"geometry": {"type": "Polygon"}},
                {"geometry": {"type": "Polygon", "coordinates": [["x", "y"]]}},
                {"geometry": "junk"},
                "junk",
            ],
        }
    )
    assert _rings_from_geojson(body) == [
        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
    ]


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"features": "x"}', '{"type": "FeatureCollection"}']
)
def test_rings_from_geojson_returns_nothing_for_non_collections(body: str):
    assert _rings_from_geojson(body) == []


# ---------------------------------------------------------------------------
# Region scope
# ---------------------------------------------------------------------------


async def test_region_scope_builds_one_alert_from_the_dashboard():
    alerts, session = await _fetch(
        _region_responses(), {CONF_ZONE_ID: _ARS}, {CONF_LANGUAGE: "de"}
    )
    (alert,) = alerts
    assert alert.provider == "bbk"
    assert alert.id == compute_bbk_id(_DWD_DASH_ID)
    assert alert.identifier == _DWD_DASH_ID
    assert alert.url == _warning_url(_DWD_DASH_ID)
    assert alert.msg_type == "Alert"
    assert alert.severity == "Moderate"
    assert alert.category == "Met"
    assert alert.response_type == "Prepare"
    assert alert.sender == "opendata@dwd.de"
    assert alert.sender_name == "Deutscher Wetterdienst"
    assert alert.expires == "2026-09-20T20:00:00+02:00"
    assert alert.references == ()
    # German configured (the coordinator always resolves ``auto`` to a tag).
    assert alert.language == "de-DE"
    assert alert.headline == "Amtliche WARNUNG vor STURMBÖEN"
    assert alert.language_alt == "en"
    assert alert.headline_alt == "Official WARNING of GALE-FORCE GUSTS"
    assert alert.event_alt == "gale-force gusts"
    assert alert.area_desc.endswith("und 229 weitere.")
    assert alert.parameters is not None
    assert alert.parameters["GROUP"] == "WIND"
    assert alert.parameters["bbk_channel"] == "dwd"
    assert alert.geometry is not None and alert.geometry["type"] == "MultiPolygon"
    assert _bbox_from_geometry(alert.geometry) == (11.8464, 51.6793, 14.6391, 53.1756)
    assert alert.geocodes == {}
    assert session.requested[0] == BBK_DASHBOARD_URL.format(ars=_ARS)


async def test_region_scope_with_nothing_live_is_a_quiet_day():
    responses = {BBK_DASHBOARD_URL.format(ars=_ARS): "[]"}
    alerts, session = await _fetch(responses, {CONF_ZONE_ID: _ARS})
    assert alerts == []
    assert len(session.requested) == 1


async def test_region_scope_index_failure_raises():
    responses = {BBK_DASHBOARD_URL.format(ars=_ARS): (500, "")}
    with pytest.raises(UpdateFailed, match="no index available"):
        await _fetch(responses, {CONF_ZONE_ID: _ARS})


async def test_region_scope_malformed_index_raises():
    responses = {BBK_DASHBOARD_URL.format(ars=_ARS): "{not json"}
    with pytest.raises(UpdateFailed, match="malformed JSON"):
        await _fetch(responses, {CONF_ZONE_ID: _ARS})


async def test_expired_dashboard_entries_cost_no_fetch():
    dashboard = json.loads(_fixture("bbk_dashboard.json"))
    dashboard[0]["expires"] = "2026-09-19T10:00:00+02:00"
    responses = _region_responses()
    responses[BBK_DASHBOARD_URL.format(ars=_ARS)] = json.dumps(dashboard)
    alerts, session = await _fetch(responses, {CONF_ZONE_ID: _ARS})
    assert alerts == []
    assert _warning_url(_DWD_DASH_ID) not in session.requested


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------


async def test_english_option_selects_the_english_block_with_german_alternate():
    alerts, _ = await _fetch(
        _region_responses(), {CONF_ZONE_ID: _ARS}, {CONF_LANGUAGE: "en"}
    )
    (alert,) = alerts
    assert alert.language == "en"
    assert alert.event == "gale-force gusts"
    assert alert.headline == "Official WARNING of GALE-FORCE GUSTS"
    assert alert.language_alt == "de-DE"
    assert alert.headline_alt == "Amtliche WARNUNG vor STURMBÖEN"


async def test_de_matches_dwd_de_de_by_primary_subtag():
    alerts, _ = await _fetch(
        _region_responses(), {CONF_ZONE_ID: _ARS}, {CONF_LANGUAGE: "de"}
    )
    assert alerts[0].language == "de-DE"


async def test_easy_read_german_is_selectable_where_published():
    responses = _gps_responses()
    del responses[_mapdata_url("dwd")]
    responses[_mapdata_url("dwd")] = "[]"
    alerts, _ = await _fetch(
        responses, {CONF_GPS_LOC: _IN_MOWAS}, {CONF_LANGUAGE: "de-LS"}
    )
    (alert,) = alerts
    assert alert.language == "de-LS"
    assert alert.headline == "Trinkwasserverschmutzung"
    # The alternate is English, not the other German register.
    assert alert.language_alt == "en"
    assert alert.headline_alt == "Contaminated drinking water"


async def test_easy_read_german_falls_back_to_plain_german_on_dwd():
    """DWD publishes no de-LS block; the primary subtag lands on de-DE."""
    alerts, _ = await _fetch(
        _region_responses(), {CONF_ZONE_ID: _ARS}, {CONF_LANGUAGE: "de-LS"}
    )
    assert alerts[0].language == "de-DE"


async def test_unpublished_language_falls_back_to_english():
    alerts, _ = await _fetch(
        _region_responses(), {CONF_ZONE_ID: _ARS}, {CONF_LANGUAGE: "nl"}
    )
    assert alerts[0].language == "en"


async def test_no_language_at_all_reads_english_like_wmo():
    """The coordinator always resolves ``auto``; a bare call still has a rule."""
    alerts, _ = await _fetch(_region_responses(), {CONF_ZONE_ID: _ARS})
    assert alerts[0].language == "en"


# ---------------------------------------------------------------------------
# GPS scope
# ---------------------------------------------------------------------------


async def test_gps_scope_unions_every_channel_and_keeps_the_containing_polygon():
    alerts, session = await _fetch(_gps_responses(), {CONF_GPS_LOC: _IN_MOWAS})
    for channel in BBK_CHANNELS:
        assert _mapdata_url(channel) in session.requested
    (alert,) = alerts
    assert alert.identifier == _MOW_ID
    assert alert.parameters is not None
    assert alert.parameters["bbk_channel"] == "mowas"
    assert alert.parameters["profile:DE-BBK-EVENTCODE"] == "BBK-EVC-069"
    assert alert.category == "Health"
    # MoWaS names the authority in a parameter, not in senderName.
    assert alert.sender == "DE-SL-SLS-W038"
    assert alert.sender_name == "Katastrophenschutzbehörde LK Saarlouis"
    assert alert.expires == ""
    assert alert.references == ("mow.DE-SL-SLS-W038-20260901-000",)
    assert alert.geometry is not None and alert.geometry["type"] == "Polygon"


async def test_gps_scope_keeps_the_dwd_polygon_for_a_point_inside_it():
    alerts, _ = await _fetch(_gps_responses(), {CONF_GPS_LOC: _IN_DWD_MAP})
    assert [a.identifier for a in alerts] == [_DWD_MAP_ID]


async def test_gps_scope_outside_every_polygon_is_empty():
    alerts, _ = await _fetch(_gps_responses(), {CONF_GPS_LOC: _IN_NEITHER})
    assert alerts == []


async def test_documents_the_host_cannot_serve_are_skipped():
    """Two MoWaS entries have no document (404) and one DWD entry has one."""
    responses = _gps_responses()
    alerts, session = await _fetch(responses, {CONF_GPS_LOC: _IN_MOWAS})
    assert _warning_url(_MOW_ID_2) in session.requested
    assert _warning_url(_MOW_ID_3) in session.requested
    assert [a.identifier for a in alerts] == [_MOW_ID]


async def test_non_json_and_non_cap_documents_are_skipped():
    responses = _gps_responses()
    responses[_warning_url(_MOW_ID_2)] = "<html>no</html>"
    responses[_warning_url(_MOW_ID_3)] = json.dumps({"unexpected": True})
    alerts, _ = await _fetch(responses, {CONF_GPS_LOC: _IN_MOWAS})
    assert [a.identifier for a in alerts] == [_MOW_ID]


async def test_one_failing_channel_index_is_survivable():
    responses = _gps_responses()
    responses[_mapdata_url("katwarn")] = (503, "")
    responses[_mapdata_url("lhp")] = "{bad json"
    alerts, _ = await _fetch(responses, {CONF_GPS_LOC: _IN_MOWAS})
    assert [a.identifier for a in alerts] == [_MOW_ID]


async def test_every_channel_index_failing_raises():
    responses = {_mapdata_url(c): (503, "") for c in BBK_CHANNELS}
    with pytest.raises(UpdateFailed, match="no index available"):
        await _fetch(responses, {CONF_GPS_LOC: _IN_MOWAS})


async def test_gps_scope_fails_loud_when_no_alert_carries_a_polygon():
    responses = _gps_responses()
    responses[_geojson_url(_MOW_ID)] = (500, "")
    responses[_geojson_url(_DWD_MAP_ID)] = "not json"
    with pytest.raises(UpdateFailed, match="carry no polygons"):
        await _fetch(responses, {CONF_GPS_LOC: _IN_MOWAS})


def test_gps_filter_on_an_empty_list_is_empty():
    assert BBKProvider._filter_by_polygon([], _IN_MOWAS) == []


async def test_gps_scope_rejects_unparseable_coordinates():
    with pytest.raises(UpdateFailed, match="invalid GPS"):
        await _fetch(_gps_responses(), {CONF_GPS_LOC: "nowhere"})


async def test_a_failed_geometry_fetch_ships_the_alert_without_a_shape():
    responses = _region_responses()
    responses[_geojson_url(_DWD_DASH_ID)] = (500, "")
    alerts, _ = await _fetch(responses, {CONF_ZONE_ID: _ARS}, {CONF_LANGUAGE: "de"})
    (alert,) = alerts
    assert alert.geometry is None
    assert alert.headline == "Amtliche WARNUNG vor STURMBÖEN"


async def test_expired_mapdata_entries_cost_no_fetch():
    dwd = json.loads(_fixture("bbk_mapdata_dwd.json"))
    dwd[1]["expiresDate"] = "2026-09-19T10:00:00+02:00"
    responses = _gps_responses()
    responses[_mapdata_url("dwd")] = json.dumps(dwd)
    alerts, session = await _fetch(responses, {CONF_GPS_LOC: _IN_DWD_MAP})
    assert _warning_url(_DWD_MAP_ID) in session.requested
    assert _warning_url(_DWD_MAP_ID_2) not in session.requested
    assert [a.identifier for a in alerts] == [_DWD_MAP_ID]


async def test_a_superseded_revision_in_the_same_poll_is_dropped():
    """When index and archive overlap, only the chain leaf becomes an alert."""
    successor = json.loads(_fixture("bbk_warning_mowas.json"))
    predecessor = json.loads(_fixture("bbk_warning_mowas.json"))
    predecessor["identifier"] = "mow.DE-SL-SLS-W038-20260901-000"
    predecessor.pop("references")
    responses = _gps_responses()
    responses[_mapdata_url("dwd")] = "[]"
    responses[_mapdata_url("mowas")] = json.dumps(
        [{"id": _MOW_ID}, {"id": "mow.DE-SL-SLS-W038-20260901-000"}]
    )
    responses[_warning_url(_MOW_ID)] = json.dumps(successor)
    responses[_warning_url("mow.DE-SL-SLS-W038-20260901-000")] = json.dumps(predecessor)
    alerts, session = await _fetch(responses, {CONF_GPS_LOC: _IN_MOWAS})
    assert [a.identifier for a in alerts] == [_MOW_ID]
    # The predecessor's geometry is never fetched: leaves only.
    assert _geojson_url("mow.DE-SL-SLS-W038-20260901-000") not in session.requested


async def test_a_mowas_all_clear_arrives_terminal(freezer):
    """The index lists an all-clear for six hours; it is a cancel, not an alert.

    The provider ships it as any other document — ``Cancel`` msgType, the
    predecessor in ``references``, the expiry the feed writes — and shared
    normalization makes that phase ``cancel``, which the store turns into the
    predecessor's removal (see ``test_store_supersession``).

    The clock is pinned inside the capture's six-hour window: normalization
    checks ``expires`` before ``msgType``, so on a live clock the phase turned
    ``expired`` the evening the fixture was captured.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts

    freezer.move_to("2026-09-20T16:00:00+00:00")

    responses = _gps_responses()
    responses[_mapdata_url("dwd")] = "[]"
    responses[_mapdata_url("mowas")] = json.dumps(
        [
            {
                "id": _MOW_CANCEL_ID,
                "type": "Cancel",
                "severity": "Minor",
                "expiresDate": "2026-09-20T23:50:56+02:00",
            }
        ]
    )
    responses[_warning_url(_MOW_CANCEL_ID)] = _fixture("bbk_warning_mowas_cancel.json")
    responses[_geojson_url(_MOW_CANCEL_ID)] = _fixture(
        "bbk_geojson_mowas_cancel.geojson"
    )
    alerts, _session = await _fetch(
        responses, {CONF_GPS_LOC: _IN_SIERSBURG}, {CONF_LANGUAGE: "de"}
    )
    (alert,) = alerts
    assert alert.msg_type == "Cancel"
    assert alert.response_type == "AllClear"
    assert alert.references == (_MOW_CANCELLED_ID,)
    assert alert.expires == "2026-09-20T23:50:56+02:00"
    assert alert.headline == "Entwarnung: Stromabschaltung - Siersburg"
    (normalized,) = normalize_alerts([alert])
    assert normalized.phase == "cancel"


async def test_the_shared_cache_is_used_for_documents_and_geometry():
    cache = CAPContentCache()
    session = StubSession(_region_responses())
    provider = BBKProvider()
    for _ in range(2):
        await provider.async_fetch(
            session,  # type: ignore[arg-type]
            {CONF_ZONE_ID: _ARS},
            {},
            cap_content_cache=cache,
            user_agent="test",
        )
    # Two polls: two index fetches, but one document and one geometry fetch.
    assert session.requested.count(BBK_DASHBOARD_URL.format(ars=_ARS)) == 2
    assert session.requested.count(_warning_url(_DWD_DASH_ID)) == 1
    assert session.requested.count(_geojson_url(_DWD_DASH_ID)) == 1
    assert len(cache) == 2


async def test_fetch_without_a_shared_cache_still_works():
    session = StubSession(_region_responses())
    alerts = await BBKProvider().async_fetch(
        session,  # type: ignore[arg-type]
        {CONF_ZONE_ID: _ARS},
        {},
    )
    assert len(alerts) == 1
    assert session.request_headers[0] is None


def test_provider_name():
    assert BBKProvider().name == "bbk"
