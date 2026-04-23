"""GeometryStore: put/get/delete/purge/eviction."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load_geometry_store():
    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent
    full = "cap_alerts.geometry_store"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / "geometry_store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


gs_mod = _load_geometry_store()
GeometryStore = gs_mod.GeometryStore


def _poly(n_coords: int = 100) -> dict:
    """Build a polygon with ``n_coords`` coordinate pairs."""
    coords = [[i * 1.0, i * 1.0] for i in range(n_coords)]
    return {"type": "Polygon", "coordinates": [coords]}


@pytest.mark.asyncio
async def test_put_get_roundtrip():
    store = GeometryStore()
    geom = {"type": "Point", "coordinates": [-75.0, 35.0]}
    await store.put("nws:a", geom)
    assert await store.get("nws:a") == geom


@pytest.mark.asyncio
async def test_get_missing_returns_none():
    store = GeometryStore()
    assert await store.get("nws:missing") is None


@pytest.mark.asyncio
async def test_delete_noop_on_missing():
    store = GeometryStore()
    await store.delete("nws:missing")  # should not raise


@pytest.mark.asyncio
async def test_delete_removes_entry():
    store = GeometryStore()
    await store.put("nws:a", {"type": "Point", "coordinates": [0, 0]})
    await store.delete("nws:a")
    assert await store.get("nws:a") is None


@pytest.mark.asyncio
async def test_purge_missing_scoped_to_prefix():
    store = GeometryStore()
    await store.put("nws:a", {"type": "Point", "coordinates": [0, 0]})
    await store.put("nws:b", {"type": "Point", "coordinates": [1, 1]})
    await store.put("eccc:x", {"type": "Point", "coordinates": [2, 2]})

    await store.purge_missing({"nws:a"}, prefix="nws:")

    assert await store.get("nws:a") is not None
    assert await store.get("nws:b") is None
    # eccc untouched because prefix was nws:
    assert await store.get("eccc:x") is not None


@pytest.mark.asyncio
async def test_eviction_under_byte_cap(monkeypatch):
    monkeypatch.setattr(gs_mod, "MAX_BYTES", 2_000)
    store = GeometryStore()
    for i in range(10):
        await store.put(f"nws:{i}", _poly(100))
    # Oldest entries should have been evicted.
    present = [i for i in range(10) if await store.get(f"nws:{i}") is not None]
    assert len(present) < 10
    # Most recent writes are retained.
    assert 9 in present


@pytest.mark.asyncio
async def test_put_update_overwrites_same_key():
    store = GeometryStore()
    g1 = {"type": "Point", "coordinates": [0, 0]}
    g2 = {"type": "Point", "coordinates": [9, 9]}
    await store.put("nws:a", g1)
    await store.put("nws:a", g2)
    assert await store.get("nws:a") == g2


def test_store_does_not_import_homeassistant_storage():
    """Regression guard: the in-memory store must not pull in Store."""
    # Drop any cached import so we exercise a fresh load.
    sys.modules.pop("cap_alerts.geometry_store", None)
    sys.modules.pop("homeassistant.helpers.storage", None)

    spec = importlib.util.spec_from_file_location(
        "cap_alerts.geometry_store", _PKG_DIR / "geometry_store.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cap_alerts.geometry_store"] = mod
    spec.loader.exec_module(mod)

    assert "homeassistant.helpers.storage" not in sys.modules
