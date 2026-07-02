"""Tests for MeteoAlarm country-source resolution (Part B).

Covers the pure ``_resolve_country_code`` normalizer and the coordinator's
``_resolve_config`` country-source path. Reuses the isolation loader from
``test_coordinator_tracker.py`` so no Home Assistant runtime is needed; the
coordinator instance is built via ``object.__new__`` to skip the heavy
``__init__`` (provider/store wiring) the resolution path doesn't touch.
"""

from __future__ import annotations

import pytest

from tests.test_coordinator_tracker import _load_coordinator

coordinator = _load_coordinator()
_resolve_country_code = coordinator._resolve_country_code
AlertsDataUpdateCoordinator = coordinator.AlertsDataUpdateCoordinator

from cap_alerts.const import (  # noqa: E402
    CONF_COUNTRY,
    CONF_COUNTRY_ATTRIBUTE,
    CONF_COUNTRY_ENTITY,
    CONF_PROVIDER,
)


# --- normalizer --------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("FR", "FR"),
        ("fr", "FR"),
        (" fr ", "FR"),
        ("France", "FR"),
        ("france", "FR"),
        ("United Kingdom", "UK"),
        ("  Germany  ", "DE"),
        # Code aliases: ISO 3166 "GB" and EU "EL" map to MeteoAlarm codes.
        ("GB", "UK"),
        ("gb", "UK"),
        ("EL", "GR"),
        ("XX", None),
        ("Atlantis", None),
        ("", None),
        ("   ", None),
        (None, None),
        # Non-string values (a country attribute can hold anything).
        (123, None),
        (["FR"], None),
        ({"country": "FR"}, None),
    ],
)
def test_resolve_country_code(value, expected):
    assert _resolve_country_code(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        # BigDataCloud countryName values (GeoLocator's backend), verified
        # 2026-07-02 — the ISO official name, not the common short name.
        ("United Kingdom of Great Britain and Northern Ireland (the)", "UK"),
        ("Netherlands (Kingdom of the)", "NL"),
        ("Moldova (the Republic of)", "MD"),
        ("North Macedonia", "MK"),
        ("Czechia", "CZ"),
        # Common variants from other geocoders.
        ("Czech Republic", "CZ"),
        ("Great Britain", "UK"),
        ("Republic of Moldova", "MD"),
        ("republic of north macedonia", "MK"),
    ],
)
def test_resolve_country_code_geocoder_names(value, expected):
    assert _resolve_country_code(value) == expected


# --- coordinator resolution --------------------------------------------------


class _State:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


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


def test_country_from_entity_state():
    coord = _make_coord(
        {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY_ENTITY: "sensor.geo"},
        {"sensor.geo": _State("France")},
    )
    config, _options = coord._resolve_config()
    assert config[CONF_COUNTRY] == "FR"


def test_country_from_entity_attribute():
    coord = _make_coord(
        {
            CONF_PROVIDER: "meteoalarm",
            CONF_COUNTRY_ENTITY: "sensor.geo",
            CONF_COUNTRY_ATTRIBUTE: "iso",
        },
        {"sensor.geo": _State("Home", {"iso": "DE"})},
    )
    config, _options = coord._resolve_config()
    assert config[CONF_COUNTRY] == "DE"


@pytest.mark.parametrize("bad_state", ["unavailable", "unknown", ""])
def test_unresolvable_state_leaves_country_unset(bad_state):
    coord = _make_coord(
        {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY_ENTITY: "sensor.geo"},
        {"sensor.geo": _State(bad_state)},
    )
    config, _options = coord._resolve_config()
    assert CONF_COUNTRY not in config


def test_missing_entity_leaves_country_unset():
    coord = _make_coord(
        {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY_ENTITY: "sensor.geo"},
        {},  # entity not present
    )
    config, _options = coord._resolve_config()
    assert CONF_COUNTRY not in config


def test_unmapped_value_leaves_country_unset():
    coord = _make_coord(
        {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY_ENTITY: "sensor.geo"},
        {"sensor.geo": _State("Atlantis")},
    )
    config, _options = coord._resolve_config()
    assert CONF_COUNTRY not in config


def test_non_string_attribute_leaves_country_unset():
    # An attribute can hold any JSON type; resolution must degrade cleanly
    # instead of raising AttributeError inside the coordinator.
    coord = _make_coord(
        {
            CONF_PROVIDER: "meteoalarm",
            CONF_COUNTRY_ENTITY: "sensor.geo",
            CONF_COUNTRY_ATTRIBUTE: "codes",
        },
        {"sensor.geo": _State("Home", {"codes": ["FR", "DE"]})},
    )
    config, _options = coord._resolve_config()
    assert CONF_COUNTRY not in config
