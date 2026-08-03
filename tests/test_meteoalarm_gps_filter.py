"""MeteoAlarm fetch behavior: polygon filter, region picker, region fetch."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _ensure_update_coordinator_stub() -> None:
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is None:
        return
    if hasattr(helpers, "update_coordinator"):
        return
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        """Test stub of homeassistant.helpers.update_coordinator.UpdateFailed."""

    class CoordinatorEntity:
        """Test stub of homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    CoordinatorEntity.__class_getitem__ = classmethod(  # type: ignore[attr-defined]
        lambda cls, _i: cls
    )
    uc.UpdateFailed = UpdateFailed
    uc.CoordinatorEntity = CoordinatorEntity
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc  # type: ignore[attr-defined]


def _load_meteoalarm():
    full = "cap_alerts.providers.meteoalarm"
    if full in sys.modules:
        return sys.modules[full]
    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent
    if "cap_alerts.const" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "cap_alerts.const", _PKG_DIR / "const.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cap_alerts.const"] = mod
        spec.loader.exec_module(mod)
    if "cap_alerts.providers" not in sys.modules:
        providers_pkg = types.ModuleType("cap_alerts.providers")
        providers_pkg.__path__ = [str(_PKG_DIR / "providers")]
        sys.modules["cap_alerts.providers"] = providers_pkg
    _ensure_update_coordinator_stub()
    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / "meteoalarm.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


meteoalarm = _load_meteoalarm()


def _make_payload(*, status: str = "Actual") -> dict:
    return {
        "warnings": [
            {
                "uuid": "uuid-1",
                "alert": {
                    "identifier": "id-1",
                    "sender": "test@example.com",
                    "sent": "2026-04-24T08:00:00Z",
                    "status": status,
                    "msgType": "Alert",
                    "scope": "Public",
                    "info": [
                        {
                            "language": "en",
                            "category": ["Met"],
                            "event": "Wind",
                            "severity": "Moderate",
                            "urgency": "Immediate",
                            "certainty": "Likely",
                            "expires": "2026-04-24T20:00:00Z",
                            "headline": "Test wind warning",
                            "area": [
                                {
                                    "areaDesc": "Test",
                                    "geocode": [
                                        {"valueName": "EMMA_ID", "value": "DE100"},
                                    ],
                                },
                            ],
                        }
                    ],
                },
            }
        ]
    }


class _FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return json.loads(self._body)


class _FakeSession:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload)
        self._status = status
        self.last_url: str | None = None

    def get(self, url: str):
        self.last_url = url
        return _FakeResponse(self._body, self._status)


class _RoutedFakeSession:
    """Routes URL substring matches to canned response bodies/statuses."""

    def __init__(self, routes: dict[str, tuple[dict | str, int]]):
        self._routes = routes
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        for needle, (body, status) in self._routes.items():
            if needle in url:
                payload = body if isinstance(body, str) else json.dumps(body)
                return _FakeResponse(payload, status)
        return _FakeResponse("{}", 404)


@pytest.mark.asyncio
async def test_country_mode_returns_alert():
    session = _FakeSession(_make_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE"},
        options={"language": "en"},
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Wind"


@pytest.mark.asyncio
async def test_test_status_filtered_out():
    session = _FakeSession(_make_payload(status="Test"))
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE"},
        options={"language": "en"},
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_feed_url_uses_country_name_slug():
    session = _FakeSession(_make_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    await provider.async_fetch(
        session,
        config={"country": "DE"},
        options={"language": "en"},
    )
    assert session.last_url is not None
    assert session.last_url.endswith("/api/v1/warnings/feeds-germany")


@pytest.mark.asyncio
async def test_unsupported_country_raises():
    session = _FakeSession(_make_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    with pytest.raises(Exception) as excinfo:
        await provider.async_fetch(
            session,
            config={"country": "ZZ"},
            options={},
        )
    assert "unsupported country" in str(excinfo.value).lower()


# ── gps-polygon mode ────────────────────────────────────────────────


def _polygon_payload() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "meteoalarm_with_polygons.json").read_text(encoding="utf-8")
    )


@pytest.mark.asyncio
async def test_gps_polygon_keeps_warnings_inside_polygon():
    session = _FakeSession(_polygon_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    # Point inside warning A's triangle (49,9)-(51,9)-(50,11) around 50N 10E.
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "50.0,10.0"},
        options={"language": "en"},
    )
    events = {a.event for a in alerts}
    assert events == {"Test Triangle"}


@pytest.mark.asyncio
async def test_gps_polygon_keeps_multipolygon_alert_when_any_part_matches():
    session = _FakeSession(_polygon_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    # Point inside warning B's Italy polygon (41,12)-(42,12)-(41.5,13).
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "41.4,12.4"},
        options={"language": "en"},
    )
    events = {a.event for a in alerts}
    assert events == {"Two Areas"}


@pytest.mark.asyncio
async def test_gps_polygon_drops_warnings_outside_polygon():
    session = _FakeSession(_polygon_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    # Point far from any polygon.
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "0.0,0.0"},
        options={"language": "en"},
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_gps_polygon_fail_loud_when_zero_polygons():
    payload = json.loads(
        (_FIXTURE_DIR / "meteoalarm_de.json").read_text(encoding="utf-8")
    )
    session = _FakeSession(payload)
    provider = meteoalarm.MeteoAlarmProvider()

    from homeassistant.helpers.update_coordinator import UpdateFailed

    with pytest.raises(UpdateFailed) as excinfo:
        await provider.async_fetch(
            session,
            config={"country": "DE", "gps_loc": "50.0,10.0"},
            options={"language": "en"},
        )
    msg = str(excinfo.value)
    assert "DE" in msg
    assert "warnings carry no polygons" in msg
    # The message steers polygonless countries toward region-picker mode.
    assert "region-picker" in msg


@pytest.mark.asyncio
async def test_gps_polygon_mobile_falls_back_to_country_wide():
    # Fully-mobile mode (country_entity present) roaming into a country that
    # publishes no per-warning geometry degrades to country-wide instead of
    # raising UpdateFailed.
    payload = json.loads(
        (_FIXTURE_DIR / "meteoalarm_de.json").read_text(encoding="utf-8")
    )
    session = _FakeSession(payload)
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={
            "country": "DE",
            "gps_loc": "50.0,10.0",
            "country_entity": "sensor.geolocator_country",
        },
        options={"language": "en"},
    )
    assert len(alerts) >= 1


@pytest.mark.asyncio
async def test_gps_polygon_mobile_keeps_polygonless_warnings():
    # Mixed feed: in mobile mode a warning without geometry (possibly a
    # nationwide severe warning) is kept alongside the polygon match, and
    # non-matching polygons are still dropped. The fixture carries
    # "Test Triangle" (contains the point), "Two Areas" (doesn't), and
    # "No Polygon" (no geometry).
    session = _FakeSession(_polygon_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={
            "country": "DE",
            "gps_loc": "50.0,10.0",
            "country_entity": "sensor.geolocator_country",
        },
        options={"language": "en"},
    )
    events = {a.event for a in alerts}
    assert events == {"Test Triangle", "No Polygon"}


@pytest.mark.asyncio
async def test_gps_polygon_fixed_mode_drops_polygonless_warnings():
    # Fixed-country GPS mode keeps strict filtering: "No Polygon" is
    # dropped when other warnings in the feed carry geometry (also covered
    # by test_gps_polygon_keeps_warnings_inside_polygon; kept explicit as
    # the mobile test's counterpart).
    session = _FakeSession(_polygon_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "50.0,10.0"},
        options={"language": "en"},
    )
    events = {a.event for a in alerts}
    assert events == {"Test Triangle"}


@pytest.mark.asyncio
async def test_gps_polygon_quiet_feed_returns_empty():
    session = _FakeSession({"warnings": []})
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "50.0,10.0"},
        options={"language": "en"},
    )
    assert alerts == []


# ── region-picker mode ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_region_picker_intersects_geocodes():
    session = _FakeSession(_make_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "regions": ["DE100"]},
        options={"language": "en"},
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_region_picker_drops_when_no_intersection():
    session = _FakeSession(_make_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "regions": ["DE999"]},
        options={"language": "en"},
    )
    assert alerts == []


def _fr_payload() -> dict:
    return json.loads((_FIXTURE_DIR / "meteoalarm_fr.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_region_picker_intersects_nuts3():
    # France filters on NUTS3 department codes (#25). Selecting FR614 keeps
    # only the warning covering Lot-et-Garonne.
    #
    # ``now`` is inside the fixture's window: the MeteoFrance episode merge
    # drops forecast days that have already finished, so without it this
    # asserts nothing once the fixture's dates fall into the past.
    session = _FakeSession(_fr_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "FR", "regions": ["FR614"]},
        options={"language": "fr"},
        now=datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
    )
    assert len(alerts) == 1
    # The bulletin is exploded to the configured department, so the surviving
    # alert's geocodes are scoped to FR614 alone rather than its full set.
    assert alerts[0].geocodes["NUTS3"] == ("FR614",)


@pytest.mark.asyncio
async def test_region_picker_drops_when_no_nuts3_intersection():
    session = _FakeSession(_fr_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "FR", "regions": ["FR999"]},
        options={"language": "fr"},
    )
    assert alerts == []


def test_emma_id_preferred_over_secondary_schemes():
    # An area carrying both EMMA_ID and WARNCELLID resolves to the EMMA_ID for
    # region selection; the sub-region cell id is stored but never offered.
    info = {
        "area": [
            {
                "areaDesc": "Erzgebirgskreis",
                "geocode": [
                    {"valueName": "WARNCELLID", "value": "114521000"},
                    {"valueName": "EMMA_ID", "value": "DE343"},
                ],
            }
        ]
    }
    pairs = meteoalarm._region_pairs(info)
    assert pairs == [("DE343", "Erzgebirgskreis")]
    codes = meteoalarm._region_codes(
        {"EMMA_ID": ("DE343",), "WARNCELLID": ("114521000",)}
    )
    assert codes == ("DE343",)


def test_region_pairs_areadesc_fallback():
    # Named area with no region-selectable scheme falls back to areaDesc so the
    # picker is still populated for polygon-only-but-named feeds.
    info = {"area": [{"areaDesc": "Ticino", "polygon": "46,8 47,8 46.5,9"}]}
    assert meteoalarm._region_pairs(info) == [("Ticino", "Ticino")]
    # Unnamed and schemeless → nothing to offer.
    assert meteoalarm._region_pairs({"area": [{"polygon": "46,8 47,8 46.5,9"}]}) == []


# ── fetch_regions_for_country ──────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_regions_uses_endpoint():
    regions_payload = json.loads(
        (_FIXTURE_DIR / "meteoalarm_regions_de.json").read_text(encoding="utf-8")
    )
    session = _RoutedFakeSession({"/api/v1/regions/": (regions_payload, 200)})
    regions = await meteoalarm.fetch_regions_for_country(session, "DE")
    codes = [code for code, _label in regions]
    assert "DE100" in codes
    assert "DE500" in codes
    # Sorted by label, case-insensitive.
    labels = [label for _code, label in regions]
    assert labels == sorted(labels, key=str.lower)


@pytest.mark.asyncio
async def test_fetch_regions_falls_back_to_warnings():
    warnings_payload = json.loads(
        (_FIXTURE_DIR / "meteoalarm_de.json").read_text(encoding="utf-8")
    )
    session = _RoutedFakeSession(
        {
            "/api/v1/regions/": ({}, 404),
            "/api/v1/warnings/": (warnings_payload, 200),
        }
    )
    regions = await meteoalarm.fetch_regions_for_country(session, "DE")
    codes = {code for code, _label in regions}
    # Fixture has multiple EMMA_IDs starting with DE.
    assert codes
    for code in codes:
        assert code.startswith("DE")


@pytest.mark.asyncio
async def test_fetch_regions_nuts3_from_warnings():
    # France has no regions endpoint; the picker is derived from the warnings
    # feed's NUTS3 areas (#25). Pairs use department names as labels and are
    # sorted case-insensitively by label.
    warnings_payload = json.loads(
        (_FIXTURE_DIR / "meteoalarm_fr.json").read_text(encoding="utf-8")
    )
    session = _RoutedFakeSession(
        {
            "/api/v1/regions/": ({}, 404),
            "/api/v1/warnings/": (warnings_payload, 200),
        }
    )
    regions = await meteoalarm.fetch_regions_for_country(session, "FR")
    assert ("FR614", "Lot-et-Garonne") in regions
    codes = {code for code, _label in regions}
    assert {"FR614", "FR611", "FR631", "FR615"} <= codes
    for code in codes:
        assert code.startswith("FR")
    labels = [label for _code, label in regions]
    assert labels == sorted(labels, key=str.lower)


@pytest.mark.asyncio
async def test_fetch_regions_raises_when_both_paths_fail():
    session = _RoutedFakeSession(
        {
            "/api/v1/regions/": ({}, 500),
            "/api/v1/warnings/": ({}, 500),
        }
    )
    from homeassistant.helpers.update_coordinator import UpdateFailed

    with pytest.raises(UpdateFailed):
        await meteoalarm.fetch_regions_for_country(session, "DE")


# ── MeteoFrance green "no warning" markers (issue #37) ─────────────


def _fr_green_markers_payload() -> dict:
    return json.loads(
        (_FIXTURE_DIR / "meteoalarm_fr_green_markers.json").read_text(encoding="utf-8")
    )


# Inside the window of both real bulletins in that fixture (the orages one runs
# 2026-08-03T16:04+02:00 → 2026-08-04T00:00+02:00).
_GREEN_MARKERS_NOW = datetime(2026, 8, 3, 18, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_fetch_drops_green_markers_in_region_mode():
    # End to end: the fixture's four warnings are one real canicule bulletin,
    # its zero-length green twin, one real orages bulletin, and its supersede
    # marker. Both markers share an id with the bulletin they refer to, so
    # without the drop the store keeps whichever arrives last.
    #
    # ``now`` sits inside both bulletins' windows: the episode merge drops
    # finished forecast days, so leaving it to the wall clock makes this pass
    # or fail depending on the hour the suite runs.
    session = _FakeSession(_fr_green_markers_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "FR", "regions": ["FR101"]},
        options={"language": "fr"},
        now=_GREEN_MARKERS_NOW,
    )
    assert len(alerts) == 2
    assert len({a.id for a in alerts}) == 2
    for a in alerts:
        assert a.parameters["awareness_level"].startswith("2;")


@pytest.mark.asyncio
async def test_fetch_drops_green_markers_in_country_mode():
    session = _FakeSession(_fr_green_markers_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "FR"},
        options={"language": "fr"},
        now=_GREEN_MARKERS_NOW,
    )
    assert len(alerts) == 2
    assert {a.event for a in alerts} == {
        "Vigilance jaune canicule",
        "Vigilance jaune orages",
    }
