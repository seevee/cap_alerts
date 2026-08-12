"""Unit tests for the coordinator's pure tracker-resolution helper.

No Home Assistant runtime is started: ``_resolve_tracker_gps`` is a module-level
function and is called directly.
"""

from __future__ import annotations

import pytest

from custom_components.cap_alerts import coordinator
from custom_components.cap_alerts.const import (
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_TRACKER_ENTITY,
)

_resolve_tracker_gps = coordinator._resolve_tracker_gps
UpdateFailed = coordinator.UpdateFailed
AlertsDataUpdateCoordinator = coordinator.AlertsDataUpdateCoordinator


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
    coord._tracker_resolve_warned = False
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


def test_unresolvable_tracker_warns_once_per_failure_streak(caplog):
    # A day-long outage at a short poll interval must not emit one warning
    # per poll; the guard resets once the tracker resolves again.
    coord = _make_coord(
        {CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.phone"},
        {},
    )
    with caplog.at_level("WARNING"):
        for _ in range(3):
            with pytest.raises(UpdateFailed):
                coord._resolve_config()
    assert sum("has no location" in r.message for r in caplog.records) == 1

    # Tracker recovers → guard resets → next streak warns again.
    coord.hass = _Hass(
        {"device_tracker.phone": _State({"latitude": 1.0, "longitude": 2.0})}
    )
    coord._resolve_config()
    coord.hass = _Hass({})
    caplog.clear()
    with caplog.at_level("WARNING"):
        with pytest.raises(UpdateFailed):
            coord._resolve_config()
    assert sum("has no location" in r.message for r in caplog.records) == 1
