"""Coordinator writes and purges geometry in lockstep with alert lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load_geometry_store():
    from tests.test_geometry_store import _load_geometry_store as _gs  # type: ignore

    return _gs()


gs_mod = _load_geometry_store()
GeometryStore = gs_mod.GeometryStore


# Reproduce the geometry put/purge step of _async_update_data in isolation so
# we don't need to spin up HA or the provider layer. Mirrors coordinator.py.
async def _apply_cycle(store, alerts, provider_name: str) -> None:
    active_refs: set[str] = set()
    for a in alerts:
        if a.get("geometry_ref") and a.get("geometry"):
            await store.put(a["geometry_ref"], a["geometry"])
            active_refs.add(a["geometry_ref"])
    await store.purge_missing(active_refs, prefix=f"{provider_name}:")


@pytest.mark.asyncio
async def test_add_then_clear_lifecycle():
    store = GeometryStore()
    geom_a = {"type": "Point", "coordinates": [0, 0]}
    geom_b = {"type": "Point", "coordinates": [1, 1]}

    # Cycle 1: two alerts.
    await _apply_cycle(
        store,
        [
            {"geometry_ref": "nws:a", "geometry": geom_a},
            {"geometry_ref": "nws:b", "geometry": geom_b},
        ],
        provider_name="nws",
    )
    assert await store.get("nws:a") == geom_a
    assert await store.get("nws:b") == geom_b

    # Cycle 2: only 'a' remains.
    await _apply_cycle(
        store,
        [{"geometry_ref": "nws:a", "geometry": geom_a}],
        provider_name="nws",
    )
    assert await store.get("nws:a") == geom_a
    assert await store.get("nws:b") is None

    # Cycle 3: empty. 'a' purged too.
    await _apply_cycle(store, [], provider_name="nws")
    assert await store.get("nws:a") is None


@pytest.mark.asyncio
async def test_cross_provider_isolation():
    """One provider's empty poll must not wipe another provider's refs."""
    store = GeometryStore()
    geom = {"type": "Point", "coordinates": [0, 0]}

    await _apply_cycle(
        store, [{"geometry_ref": "eccc:x", "geometry": geom}], provider_name="eccc"
    )
    # NWS polls empty — must not touch eccc:x.
    await _apply_cycle(store, [], provider_name="nws")
    assert await store.get("eccc:x") == geom


@pytest.mark.asyncio
async def test_update_same_ref_overwrites():
    store = GeometryStore()
    g1 = {"type": "Point", "coordinates": [0, 0]}
    g2 = {"type": "Point", "coordinates": [9, 9]}

    await _apply_cycle(
        store, [{"geometry_ref": "nws:a", "geometry": g1}], provider_name="nws"
    )
    await _apply_cycle(
        store, [{"geometry_ref": "nws:a", "geometry": g2}], provider_name="nws"
    )
    assert await store.get("nws:a") == g2
