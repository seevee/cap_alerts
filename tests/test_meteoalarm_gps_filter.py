"""MeteoAlarm GPS-mode point-in-polygon filtering."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


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


# Square covering Berlin: roughly lat 52..53, lon 13..14.
BERLIN_SQUARE = [[13.0, 52.0], [14.0, 52.0], [14.0, 53.0], [13.0, 53.0], [13.0, 52.0]]


def test_point_inside_polygon():
    # Berlin (52.52, 13.40) is inside.
    assert meteoalarm._point_in_polygon(52.52, 13.40, BERLIN_SQUARE)


def test_point_outside_polygon():
    # Madrid (40.41, -3.70) is outside.
    assert not meteoalarm._point_in_polygon(40.41, -3.70, BERLIN_SQUARE)


def test_polygon_text_round_trip():
    # CAP polygon order: lat,lon. Provider returns [lon, lat].
    text = "52.0,13.0 53.0,13.0 53.0,14.0 52.0,14.0 52.0,13.0"
    coords = meteoalarm._parse_polygon_text(text)
    assert coords is not None
    # First point is [13.0, 52.0]
    assert coords[0] == [13.0, 52.0]


def test_malformed_polygon_text_returns_none():
    assert meteoalarm._parse_polygon_text("") is None
    assert meteoalarm._parse_polygon_text("52.0") is None
    assert meteoalarm._parse_polygon_text("not,a,number") is None


def _make_entry_xml(*, with_polygon: bool, status: str = "Actual") -> str:
    poly = (
        "<cap:polygon>52.0,13.0 53.0,13.0 53.0,14.0 52.0,14.0 52.0,13.0</cap:polygon>"
        if with_polygon
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <entry>
    <id>urn:test:1</id>
    <updated>2026-04-24T08:00:00Z</updated>
    <cap:identifier>id-1</cap:identifier>
    <cap:status>{status}</cap:status>
    <cap:msgType>Alert</cap:msgType>
    <cap:info>
      <cap:language>en-GB</cap:language>
      <cap:event>Wind</cap:event>
      <cap:severity>Moderate</cap:severity>
      <cap:area>
        <cap:areaDesc>Test</cap:areaDesc>
        {poly}
      </cap:area>
    </cap:info>
  </entry>
</feed>"""


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self._text = text
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, body: str, status: int = 200):
        self._body = body
        self._status = status
        self.last_url: str | None = None

    def get(self, url: str):
        self.last_url = url
        return _FakeResponse(self._body, self._status)


@pytest.mark.asyncio
async def test_gps_inside_polygon_includes_alert():
    body = _make_entry_xml(with_polygon=True)
    session = _FakeSession(body)
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "52.52,13.40"},
        options={"language": "en"},
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_gps_outside_polygon_excludes_alert():
    body = _make_entry_xml(with_polygon=True)
    session = _FakeSession(body)
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "40.41,-3.70"},
        options={"language": "en"},
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_gps_mode_excludes_entry_without_polygon():
    body = _make_entry_xml(with_polygon=False)
    session = _FakeSession(body)
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE", "gps_loc": "52.52,13.40"},
        options={"language": "en"},
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_country_mode_includes_entry_without_polygon():
    body = _make_entry_xml(with_polygon=False)
    session = _FakeSession(body)
    provider = meteoalarm.MeteoAlarmProvider()
    alerts = await provider.async_fetch(
        session,
        config={"country": "DE"},  # no gps_loc → country mode
        options={"language": "en"},
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_feed_url_contains_lowercase_country():
    body = _make_entry_xml(with_polygon=True)
    session = _FakeSession(body)
    provider = meteoalarm.MeteoAlarmProvider()
    await provider.async_fetch(
        session,
        config={"country": "DE"},
        options={"language": "en"},
    )
    assert session.last_url is not None
    assert session.last_url.endswith("meteoalarm-legacy-atom-de")
