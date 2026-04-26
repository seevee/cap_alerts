"""Tests for ``_compute_device_title`` across all provider/filter modes."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _ensure_module(name: str, is_pkg: bool = False) -> types.ModuleType:
    """Get-or-create a module; idempotent across test files."""
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    if is_pkg:
        mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    """Set an attribute only if it's not already present."""
    if not hasattr(mod, name):
        setattr(mod, name, value)


def _stub_modules() -> None:
    """Provide minimal stubs so ``config_flow.py`` can be imported.

    Stubs are *additive*: if another test (e.g. ``test_sync_entities.py``)
    has already populated ``homeassistant.*`` with overlapping attributes,
    we add only what's missing instead of re-stubbing.
    """
    vol = _ensure_module("voluptuous")
    _ensure_attr(vol, "Schema", lambda x=None: x)
    _ensure_attr(vol, "Required", lambda *a, **kw: a[0] if a else None)
    _ensure_attr(vol, "Optional", lambda *a, **kw: a[0] if a else None)
    _ensure_attr(vol, "All", lambda *a, **kw: None)
    _ensure_attr(vol, "Coerce", lambda *a, **kw: None)
    _ensure_attr(vol, "Range", lambda *a, **kw: None)
    _ensure_attr(vol, "In", lambda *a, **kw: None)

    _ensure_module("homeassistant", is_pkg=True)

    ce = _ensure_module("homeassistant.config_entries")

    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs: object) -> None:
            pass

    _ensure_attr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    _ensure_attr(ce, "ConfigFlow", _ConfigFlow)
    _ensure_attr(ce, "ConfigFlowResult", dict)
    _ensure_attr(ce, "OptionsFlow", type("OptionsFlow", (), {}))

    core = _ensure_module("homeassistant.core")
    _ensure_attr(core, "callback", lambda f: f)

    _ensure_module("homeassistant.helpers", is_pkg=True)

    aclient = _ensure_module("homeassistant.helpers.aiohttp_client")
    _ensure_attr(aclient, "async_get_clientsession", lambda hass: None)

    selector = _ensure_module("homeassistant.helpers.selector")
    _ensure_attr(selector, "EntitySelector", type("EntitySelector", (), {}))
    _ensure_attr(selector, "EntitySelectorConfig", type("EntitySelectorConfig", (), {}))
    _ensure_attr(selector, "SelectOptionDict", dict)
    _ensure_attr(selector, "SelectSelector", type("SelectSelector", (), {}))
    _ensure_attr(selector, "SelectSelectorConfig", type("SelectSelectorConfig", (), {}))
    _ensure_attr(
        selector,
        "SelectSelectorMode",
        type("SelectSelectorMode", (), {"DROPDOWN": "dropdown"}),
    )

    coord = _ensure_module("homeassistant.helpers.update_coordinator")

    class _UpdateFailed(Exception):
        pass

    _ensure_attr(coord, "UpdateFailed", _UpdateFailed)

    # Defensive: also seed attrs that ``test_sync_entities.py`` needs.
    # Its ``_stub_ha_modules`` early-bails when ``homeassistant`` already
    # exists, so if our stubs run first, sensor.py won't find these unless
    # we provide them up front.
    _CoordinatorEntity = type("CoordinatorEntity", (), {})
    _CoordinatorEntity.__class_getitem__ = classmethod(  # type: ignore[attr-defined]
        lambda cls, _i: cls
    )
    _ensure_attr(coord, "CoordinatorEntity", _CoordinatorEntity)

    _ensure_module("homeassistant.components", is_pkg=True)
    sensor_mod = _ensure_module("homeassistant.components.sensor")
    _ensure_attr(
        sensor_mod,
        "SensorDeviceClass",
        type("SensorDeviceClass", (), {"TIMESTAMP": "timestamp"}),
    )
    _ensure_attr(sensor_mod, "SensorEntity", type("SensorEntity", (), {}))
    _ensure_attr(
        sensor_mod,
        "SensorStateClass",
        type("SensorStateClass", (), {"MEASUREMENT": "measurement"}),
    )
    const_mod = _ensure_module("homeassistant.const")
    _ensure_attr(
        const_mod,
        "EntityCategory",
        type("EntityCategory", (), {"DIAGNOSTIC": "diagnostic"}),
    )
    _ensure_attr(core, "HomeAssistant", object)

    er_mod = _ensure_module("homeassistant.helpers.entity_registry")
    _ensure_attr(
        er_mod, "async_get", lambda hass: getattr(hass, "entity_registry", None)
    )
    _ensure_attr(er_mod, "async_entries_for_config_entry", lambda reg, entry_id: [])
    dr_mod = _ensure_module("homeassistant.helpers.device_registry")
    _ensure_attr(dr_mod, "DeviceInfo", dict)

    util_mod = _ensure_module("homeassistant.util")

    def _slugify(s: str) -> str:
        return "_".join("".join(c.lower() if c.isalnum() else " " for c in s).split())

    _ensure_attr(util_mod, "slugify", _slugify)

    # ``conftest.py`` already provides the ``cap_alerts`` namespace package
    # with a working ``__path__``, so the real ``cap_alerts.providers`` and
    # ``cap_alerts.providers.meteoalarm`` modules load on demand. Stubbing
    # them here would clobber the real modules and break other test files
    # that import them (e.g. ``tests/test_meteoalarm_parser.py``).
    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent


def _load_config_flow() -> types.ModuleType:
    _stub_modules()
    full = "cap_alerts.config_flow"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / "config_flow.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


config_flow = _load_config_flow()
_compute = config_flow._compute_device_title

from cap_alerts.const import (  # noqa: E402
    CONF_COUNTRY,
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_REGION_LABELS,
    CONF_REGIONS,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
)


# --- NWS ---------------------------------------------------------------------


def test_nws_zone_title():
    assert (
        _compute({CONF_PROVIDER: "nws", CONF_ZONE_ID: "ALC001"})
        == "CAP Alerts NWS (ALC001)"
    )


def test_nws_gps_title():
    assert (
        _compute({CONF_PROVIDER: "nws", CONF_GPS_LOC: "40.7,-74.0"})
        == "CAP Alerts NWS (40.7,-74.0)"
    )


def test_nws_tracker_title():
    assert (
        _compute({CONF_PROVIDER: "nws", CONF_TRACKER_ENTITY: "device_tracker.phone"})
        == "CAP Alerts NWS (phone)"
    )


# --- ECCC --------------------------------------------------------------------


def test_eccc_province_title():
    assert (
        _compute({CONF_PROVIDER: "eccc", CONF_PROVINCE: "ON"}) == "CAP Alerts ECCC (ON)"
    )


def test_eccc_gps_title():
    assert (
        _compute({CONF_PROVIDER: "eccc", CONF_GPS_LOC: "45.4,-75.7"})
        == "CAP Alerts ECCC (45.4,-75.7)"
    )


# --- MeteoAlarm --------------------------------------------------------------


def test_meteoalarm_country_only_uses_friendly_name():
    assert (
        _compute({CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: "DE"})
        == "CAP Alerts METEOALARM (Germany)"
    )


def test_meteoalarm_gps_polygon_uses_lat_lon():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_GPS_LOC: "52.52,13.405",
    }
    assert _compute(data) == "CAP Alerts METEOALARM (52.52,13.405)"


def test_meteoalarm_region_picker_single():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_REGIONS: ["DE100"],
        CONF_REGION_LABELS: {"DE100": "Erzgebirgskreis"},
    }
    assert _compute(data) == "CAP Alerts METEOALARM (DE — Erzgebirgskreis)"


def test_meteoalarm_region_picker_multi():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_REGIONS: ["DE100", "DE200", "DE300"],
        CONF_REGION_LABELS: {
            "DE100": "Saxony",
            "DE200": "Bavaria",
            "DE300": "Hesse",
        },
    }
    # Bavaria sorts alphabetically first; +2 more.
    assert _compute(data) == "CAP Alerts METEOALARM (DE — Bavaria +2)"


def test_meteoalarm_region_picker_legacy_no_labels():
    data = {
        CONF_PROVIDER: "meteoalarm",
        CONF_COUNTRY: "DE",
        CONF_REGIONS: ["DE100", "DE200", "DE300"],
    }
    assert _compute(data) == "CAP Alerts METEOALARM (Germany — 3 regions)"
