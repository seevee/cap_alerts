"""REST view + WS command: happy path and miss."""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock

import pytest

from custom_components.cap_alerts import geometry_store as gs_mod
from custom_components.cap_alerts import views as views_mod
from custom_components.cap_alerts import websocket as websocket_mod

# ``async_response``/``websocket_command`` wrap the handler in a scheduling
# callback; unwrap back to the original coroutine function.
_ws_get_geometry = inspect.unwrap(websocket_mod._ws_get_geometry)


def _resp_payload(resp):
    """JSON payload of an aiohttp response."""
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_rest_view_returns_feature_collection():
    store = gs_mod.GeometryStore()
    geom = {"type": "Point", "coordinates": [-75.0, 35.0]}
    await store.put("nws:a", geom)

    view = views_mod.CapAlertsGeometryView(store)
    resp = await view.get(request=None, geometry_ref="nws:a")

    assert resp.status == 200
    payload = _resp_payload(resp)
    assert payload["type"] == "FeatureCollection"
    assert payload["features"][0]["geometry"] == geom
    assert payload["features"][0]["properties"]["ref"] == "nws:a"


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
    await _ws_get_geometry(hass, conn, msg)

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
    await _ws_get_geometry(hass, conn, msg)

    conn.send_error.assert_called_once()
    conn.send_result.assert_not_called()
