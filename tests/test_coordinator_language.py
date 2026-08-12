"""Coordinator resolution of the ``language`` option's ``auto`` value.

No Home Assistant runtime is started: the coordinator is built via
``object.__new__`` so ``_resolve_config`` can be exercised on its own.

The three providers that read a language deliberately resolve ``auto``
differently — ECCC to one of two full tags, MeteoAlarm to a 2-letter prefix,
WMO verbatim (issue #59) — so each branch is pinned here.
"""

from __future__ import annotations

import pytest

from custom_components.cap_alerts import coordinator
from custom_components.cap_alerts.const import CONF_LANGUAGE, CONF_PROVIDER

AlertsDataUpdateCoordinator = coordinator.AlertsDataUpdateCoordinator


class _Config:
    def __init__(self, language: str) -> None:
        self.language = language


class _Hass:
    def __init__(self, language: str) -> None:
        self.config = _Config(language)
        self.states = None


class _Entry:
    def __init__(self, data: dict, options: dict) -> None:
        self.data = data
        self.options = options


def _resolve(provider: str, ha_language: str, options: dict | None = None) -> str:
    coord = object.__new__(AlertsDataUpdateCoordinator)
    coord.hass = _Hass(ha_language)
    coord.config_entry = _Entry({CONF_PROVIDER: provider}, options or {})
    coord._tracker_resolve_warned = False
    coord._country_resolve_warned = False
    _config, resolved = coord._resolve_config()
    return resolved[CONF_LANGUAGE]


# --- WMO (issue #59) --------------------------------------------------------


@pytest.mark.parametrize(
    ("ha_language", "expected"),
    [
        ("zh-Hans", "zh-Hans"),  # not truncated to "zh"
        ("pt-BR", "pt-BR"),  # must stay distinguishable from pt-PT
        ("de", "de"),
        ("", "en"),
    ],
)
def test_wmo_auto_resolves_verbatim(ha_language: str, expected: str):
    """WMO bodies carry full tags, so the HA locale is passed through as-is."""
    assert _resolve("wmo", ha_language) == expected


def test_wmo_explicit_language_passes_through():
    assert _resolve("wmo", "en", {CONF_LANGUAGE: "zh-Hans"}) == "zh-Hans"


# --- Regression guards on the pre-existing branches --------------------------


def test_meteoalarm_auto_truncates_to_two_letters():
    assert _resolve("meteoalarm", "zh-Hans") == "zh"


def test_eccc_auto_resolves_to_french_for_a_french_locale():
    assert _resolve("eccc", "fr-BE") == "fr-CA"


def test_eccc_auto_resolves_to_english_otherwise():
    assert _resolve("eccc", "zh-Hans") == "en-CA"


def test_eccc_explicit_language_passes_through():
    assert _resolve("eccc", "fr-CA", {CONF_LANGUAGE: "en-CA"}) == "en-CA"
