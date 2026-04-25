"""MeteoAlarm fetch behavior: country slug, GPS no-op, status filter."""

from __future__ import annotations

import importlib.util
import json
import logging
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
async def test_gps_loc_does_not_filter_and_logs_warning(caplog):
    # MeteoAlarm warnings have no polygons; GPS mode degrades to country
    # mode and emits a warning so users know filtering didn't apply.
    session = _FakeSession(_make_payload())
    provider = meteoalarm.MeteoAlarmProvider()
    with caplog.at_level(
        logging.WARNING, logger="custom_components.cap_alerts.providers.meteoalarm"
    ):
        alerts = await provider.async_fetch(
            session,
            config={"country": "DE", "gps_loc": "10.0,10.0"},
            options={"language": "en"},
        )
    assert len(alerts) == 1
    assert any("GPS filter is unavailable" in r.message for r in caplog.records)


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
