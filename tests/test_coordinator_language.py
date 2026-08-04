"""Coordinator resolution of the ``language`` option's ``auto`` value.

Mirrors ``test_coordinator_tracker.py``'s import-in-isolation approach: no
Home Assistant runtime is started, and the coordinator is built via
``object.__new__`` so ``_resolve_config`` can be exercised on its own.

The three providers that read a language deliberately resolve ``auto``
differently — ECCC to one of two full tags, MeteoAlarm to a 2-letter prefix,
WMO verbatim (issue #59) — so each branch is pinned here.
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
AlertsDataUpdateCoordinator = coordinator.AlertsDataUpdateCoordinator

from cap_alerts.const import CONF_LANGUAGE, CONF_PROVIDER  # noqa: E402


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
