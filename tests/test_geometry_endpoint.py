"""REST view + WS command: happy path and miss."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _stub_http() -> None:
    if "homeassistant.components.http" in sys.modules:
        return

    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.helpers",
    ):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = []
            sys.modules[name] = mod

    # voluptuous stub — websocket.py uses it at import time for the schema decorator
    if "voluptuous" not in sys.modules:
        vol_mod = types.ModuleType("voluptuous")
        vol_mod.Required = lambda key: key
        vol_mod.Optional = lambda key, default=None: key
        vol_mod.Schema = lambda spec: spec
        sys.modules["voluptuous"] = vol_mod

    class _Resp:
        def __init__(self, payload, status):
            self.payload = payload
            self.status = status

    class HomeAssistantView:
        url: str = ""
        name: str = ""

        def json(self, data, status_code: int = 200):
            return _Resp(data, status_code)

        def json_message(self, message: str, status_code: int = 200):
            return _Resp({"message": message}, status_code)

    http_mod = types.ModuleType("homeassistant.components.http")
    http_mod.HomeAssistantView = HomeAssistantView
    sys.modules["homeassistant.components.http"] = http_mod

    # aiohttp.web.Request stub
    aiohttp_web = types.ModuleType("aiohttp.web")
    aiohttp_web.Request = object
    aiohttp_web.Response = _Resp
    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.web = aiohttp_web
        sys.modules["aiohttp"] = aiohttp
    sys.modules["aiohttp.web"] = aiohttp_web

    # websocket_api stub
    ws_api = types.ModuleType("homeassistant.components.websocket_api")

    def _noop_decorator(*_a, **_kw):
        def inner(fn):
            return fn

        return inner

    def _async_response(fn):
        return fn

    ws_api.websocket_command = _noop_decorator
    ws_api.async_response = _async_response
    ws_api.async_register_command = lambda hass, fn: None
    ws_api.ActiveConnection = object
    ws_api.ERR_NOT_FOUND = "not_found"
    sys.modules["homeassistant.components.websocket_api"] = ws_api

    # const stub — views.py imports .const
    # (already handled via local import path)

    # core stub
    core = sys.modules.setdefault(
        "homeassistant.core", types.ModuleType("homeassistant.core")
    )
    core.HomeAssistant = object
    core.callback = lambda f: f

    # Stubs other tests may rely on (sensor module, config_entries, etc.) —
    # harmless to install, keeps collection-order independent.
    if "homeassistant.components.sensor" not in sys.modules:
        sensor_mod = types.ModuleType("homeassistant.components.sensor")
        sensor_mod.SensorDeviceClass = type(
            "SensorDeviceClass", (), {"TIMESTAMP": "timestamp"}
        )
        sensor_mod.SensorEntity = type("SensorEntity", (), {})
        sensor_mod.SensorStateClass = type(
            "SensorStateClass", (), {"MEASUREMENT": "measurement"}
        )
        sys.modules["homeassistant.components.sensor"] = sensor_mod
    if "homeassistant.config_entries" not in sys.modules:
        ce = types.ModuleType("homeassistant.config_entries")
        ce.ConfigEntry = type("ConfigEntry", (), {})
        sys.modules["homeassistant.config_entries"] = ce
    if "homeassistant.const" not in sys.modules:
        const = types.ModuleType("homeassistant.const")
        const.EntityCategory = type("EntityCategory", (), {"DIAGNOSTIC": "diagnostic"})
        sys.modules["homeassistant.const"] = const
    if "homeassistant.helpers.entity_registry" not in sys.modules:
        er = types.ModuleType("homeassistant.helpers.entity_registry")
        er.async_get = lambda hass: getattr(hass, "entity_registry", None)
        er.async_entries_for_config_entry = lambda reg, entry_id: []
        sys.modules["homeassistant.helpers.entity_registry"] = er
    if "homeassistant.helpers.device_registry" not in sys.modules:
        dr = types.ModuleType("homeassistant.helpers.device_registry")
        dr.DeviceInfo = dict
        sys.modules["homeassistant.helpers.device_registry"] = dr
    if "homeassistant.helpers.update_coordinator" not in sys.modules:
        coord = types.ModuleType("homeassistant.helpers.update_coordinator")
        coord.CoordinatorEntity = type("CoordinatorEntity", (), {})
        coord.CoordinatorEntity.__class_getitem__ = classmethod(lambda cls, _i: cls)
        sys.modules["homeassistant.helpers.update_coordinator"] = coord
    if "homeassistant.util" not in sys.modules:
        util = types.ModuleType("homeassistant.util")

        def _slugify(s: str) -> str:
            return "_".join(
                "".join(c.lower() if c.isalnum() else " " for c in s).split()
            )

        util.slugify = _slugify
        sys.modules["homeassistant.util"] = util

    # cap_alerts.coordinator stub so sensor.py's `.coordinator` import resolves
    # without pulling real HA bits (matches test_sync_entities).
    if "cap_alerts.coordinator" not in sys.modules:
        coord_stub = types.ModuleType("cap_alerts.coordinator")
        coord_stub.AlertsDataUpdateCoordinator = type(
            "AlertsDataUpdateCoordinator", (), {}
        )
        sys.modules["cap_alerts.coordinator"] = coord_stub
        sys.modules["custom_components.cap_alerts.coordinator"] = coord_stub


def _load(name: str):
    _stub_http()
    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent
    # load const first since views/websocket import from it
    for mod_name in ("const", "geometry_store", "views", "websocket"):
        full = f"cap_alerts.{mod_name}"
        if full in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{mod_name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    return sys.modules[f"cap_alerts.{name}"]


views_mod = _load("views")
websocket_mod = _load("websocket")
gs_mod = _load("geometry_store")


@pytest.mark.asyncio
async def test_rest_view_returns_feature_collection():
    store = gs_mod.GeometryStore()
    geom = {"type": "Point", "coordinates": [-75.0, 35.0]}
    await store.put("nws:a", geom)

    view = views_mod.CapAlertsGeometryView(store)
    resp = await view.get(request=None, geometry_ref="nws:a")

    assert resp.status == 200
    assert resp.payload["type"] == "FeatureCollection"
    assert resp.payload["features"][0]["geometry"] == geom
    assert resp.payload["features"][0]["properties"]["ref"] == "nws:a"


@pytest.mark.asyncio
async def test_rest_view_404_on_unknown_ref():
    store = gs_mod.GeometryStore()
    view = views_mod.CapAlertsGeometryView(store)
    resp = await view.get(request=None, geometry_ref="nws:missing")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_ws_command_returns_feature_collection():
    store = gs_mod.GeometryStore()
    geom = {"type": "Point", "coordinates": [-75.0, 35.0]}
    await store.put("nws:a", geom)

    hass = MagicMock()
    hass.data = {"cap_alerts": {"geometry_store": store}}
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()

    msg = {"id": 1, "type": "cap_alerts/geometry", "geometry_ref": "nws:a"}
    await websocket_mod._ws_get_geometry(hass, conn, msg)

    conn.send_result.assert_called_once()
    _id, payload = conn.send_result.call_args.args
    assert _id == 1
    assert payload["type"] == "FeatureCollection"
    assert payload["features"][0]["geometry"] == geom
    conn.send_error.assert_not_called()


@pytest.mark.asyncio
async def test_ws_command_sends_error_on_unknown_ref():
    store = gs_mod.GeometryStore()

    hass = MagicMock()
    hass.data = {"cap_alerts": {"geometry_store": store}}
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()

    msg = {"id": 7, "type": "cap_alerts/geometry", "geometry_ref": "nws:missing"}
    await websocket_mod._ws_get_geometry(hass, conn, msg)

    conn.send_error.assert_called_once()
    conn.send_result.assert_not_called()
