"""Unit tests for the coordinator's pure tracker-resolution helper.

Mirrors the import-in-isolation approach of ``test_coordinator_geometry.py``:
no Home Assistant runtime is started. ``coordinator.py`` is loaded with a
handful of extra stubs on top of the ones ``conftest.py`` already provides,
so the module-level ``_resolve_tracker_gps`` can be exercised directly.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load_coordinator() -> types.ModuleType:
    full = "cap_alerts.coordinator"
    if full in sys.modules:
        return sys.modules[full]

    # conftest seeds homeassistant + .core + .helpers.update_coordinator
    # (UpdateFailed) + .helpers.entity_registry. Add what coordinator.py
    # additionally needs.
    ce = sys.modules.setdefault(
        "homeassistant.config_entries",
        types.ModuleType("homeassistant.config_entries"),
    )
    if not hasattr(ce, "ConfigEntry"):
        ce.ConfigEntry = type("ConfigEntry", (), {})

    const_mod = sys.modules.setdefault(
        "homeassistant.const", types.ModuleType("homeassistant.const")
    )
    const_mod.ATTR_LATITUDE = "latitude"
    const_mod.ATTR_LONGITUDE = "longitude"

    aclient = sys.modules.setdefault(
        "homeassistant.helpers.aiohttp_client",
        types.ModuleType("homeassistant.helpers.aiohttp_client"),
    )
    if not hasattr(aclient, "async_get_clientsession"):
        aclient.async_get_clientsession = lambda hass: None

    uc = sys.modules["homeassistant.helpers.update_coordinator"]
    if not hasattr(uc, "DataUpdateCoordinator"):

        class _DataUpdateCoordinator:
            def __class_getitem__(cls, _item):
                return cls

            def __init__(self, *args, **kwargs):
                pass

        uc.DataUpdateCoordinator = _DataUpdateCoordinator

    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / "coordinator.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator = _load_coordinator()
_resolve_tracker_gps = coordinator._resolve_tracker_gps
UpdateFailed = coordinator.UpdateFailed
AlertsDataUpdateCoordinator = coordinator.AlertsDataUpdateCoordinator

from cap_alerts.const import (  # noqa: E402
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_TRACKER_ENTITY,
)


class _State:
    """Minimal stand-in for an HA State (only ``.attributes`` is read)."""

    def __init__(self, attributes: dict) -> None:
        self.attributes = attributes


def test_valid_coords():
    state = _State({"latitude": 40.7128, "longitude": -74.006})
    assert _resolve_tracker_gps(state) == "40.7128,-74.006"


def test_zero_lat_lon_is_valid():
    # The equator / prime meridian must not be dropped by a truthiness check.
    state = _State({"latitude": 0.0, "longitude": 0.0})
    assert _resolve_tracker_gps(state) == "0.0,0.0"


def test_zero_latitude_nonzero_longitude():
    state = _State({"latitude": 0.0, "longitude": 12.5})
    assert _resolve_tracker_gps(state) == "0.0,12.5"


def test_missing_latitude_returns_none():
    state = _State({"longitude": -74.006})
    assert _resolve_tracker_gps(state) is None


def test_missing_longitude_returns_none():
    state = _State({"latitude": 40.7128})
    assert _resolve_tracker_gps(state) is None


def test_missing_state_returns_none():
    assert _resolve_tracker_gps(None) is None


# --- _resolve_config tracker path -------------------------------------------
#
# Guards the Part A behavior change: an unresolvable tracker raises
# UpdateFailed (entity visibly unavailable) instead of silently blanking
# CONF_GPS_LOC. Build the coordinator via object.__new__ to skip the heavy
# __init__ (provider/store wiring) the resolution path doesn't touch.


class _States:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class _Config:
    language = "en"


class _Hass:
    def __init__(self, states):
        self.states = _States(states)
        self.config = _Config()


class _Entry:
    def __init__(self, data, options=None):
        self.data = data
        self.options = options or {}


def _make_coord(data, states):
    coord = object.__new__(AlertsDataUpdateCoordinator)
    coord.hass = _Hass(states)
    coord.config_entry = _Entry(data)
    coord._country_resolve_warned = False
    return coord


def test_resolve_config_injects_gps_from_tracker():
    coord = _make_coord(
        {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.phone"},
        {"device_tracker.phone": _State({"latitude": 40.7128, "longitude": -74.006})},
    )
    config, _options = coord._resolve_config()
    assert config[CONF_GPS_LOC] == "40.7128,-74.006"


def test_resolve_config_raises_when_tracker_missing():
    coord = _make_coord(
        {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.phone"},
        {},  # entity not present
    )
    with pytest.raises(UpdateFailed):
        coord._resolve_config()


def test_resolve_config_raises_when_tracker_has_no_location():
    coord = _make_coord(
        {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.phone"},
        {"device_tracker.phone": _State({"longitude": -74.006})},  # no latitude
    )
    with pytest.raises(UpdateFailed):
        coord._resolve_config()
