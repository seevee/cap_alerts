"""GeometryStore: put/get/delete/purge/eviction."""

from __future__ import annotations

import ast
import pathlib

import pytest

from custom_components.cap_alerts import geometry_store as gs_mod
from custom_components.cap_alerts.geometry_store import GeometryStore


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
async def test_eviction_never_crosses_an_entry_boundary(caplog):
    """The budget is per entry and an entry evicts only its own refs (#197):
    one heavy scope used to push a sibling entry's polygons out of a global
    cap, silently, and the card drew an empty frame off the 404."""
    store = GeometryStore(max_bytes=2_000)
    for i in range(3):
        await store.put(f"light:wmo:{i}", _poly(20))
    for i in range(10):
        await store.put(f"heavy:gdacs:{i}", _poly(100))

    assert all(store.has(f"light:wmo:{i}") for i in range(3))
    heavy = [i for i in range(10) if store.has(f"heavy:gdacs:{i}")]
    assert 9 in heavy and 0 not in heavy
    _, heavy_bytes = store.usage("heavy")
    assert heavy_bytes <= 2_000
    assert store.usage("light")[0] == 3


@pytest.mark.asyncio
async def test_a_polygon_over_the_whole_budget_is_still_stored():
    """A put that silently stored nothing would be worse than one polygon
    over the line."""
    store = GeometryStore(max_bytes=500)
    await store.put("e:nws:small", _poly(5))
    await store.put("e:nws:huge", _poly(200))
    assert store.has("e:nws:huge")
    assert not store.has("e:nws:small")


@pytest.mark.asyncio
async def test_eviction_logs_once_per_overflow(caplog):
    """Nothing used to say so. One warning when an entry starts evicting,
    debug while it keeps doing so, and a fresh warning after a put fits."""
    store = GeometryStore(max_bytes=2_000)
    for i in range(6):
        await store.put(f"e:gdacs:{i}", _poly(100))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "entry e is over its 2000 byte polygon budget" in warnings[0].message
    assert "evicted its 1 oldest" in warnings[0].message

    # A put that fits clears the flag; the next overflow warns again.
    await store.purge_missing(set(), prefix="e:")
    await store.put("e:gdacs:a", _poly(10))
    for i in range(6):
        await store.put(f"e:gdacs:{i}", _poly(100))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2


def test_the_module_budget_is_the_default_and_stays_patchable(monkeypatch):
    monkeypatch.setattr(gs_mod, "MAX_BYTES", 123)
    assert GeometryStore().max_bytes == 123
    assert GeometryStore(max_bytes=7).max_bytes == 7


@pytest.mark.asyncio
async def test_put_update_overwrites_same_key():
    store = GeometryStore()
    g1 = {"type": "Point", "coordinates": [0, 0]}
    g2 = {"type": "Point", "coordinates": [9, 9]}
    await store.put("nws:a", g1)
    await store.put("nws:a", g2)
    assert await store.get("nws:a") == g2


def test_store_does_not_import_homeassistant_storage():
    """Regression guard: the in-memory store must not pull in HA's ``Store``.

    Read off the source rather than ``sys.modules``: under the real plugin
    ``homeassistant.helpers.storage`` is imported by something else long before
    this runs, so a runtime check would pass no matter what this module does.
    """
    tree = ast.parse(pathlib.Path(gs_mod.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(name.startswith("homeassistant") for name in imported), imported
