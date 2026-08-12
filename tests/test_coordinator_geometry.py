"""Coordinator writes and purges geometry in lockstep with alert lifecycle."""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.geometry_store import GeometryStore


# Reproduce the geometry put/purge step of _async_update_data in isolation so
# we don't need to spin up HA or the provider layer. Mirrors coordinator.py:
# refs are entry-namespaced and the purge is scoped to this entry's prefix.
async def _apply_cycle(store, alerts, entry_id: str) -> None:
    active_refs: set[str] = set()
    for a in alerts:
        if a.get("geometry_ref") and a.get("geometry"):
            await store.put(a["geometry_ref"], a["geometry"])
            active_refs.add(a["geometry_ref"])
    await store.purge_missing(active_refs, prefix=f"{entry_id}:")


@pytest.mark.asyncio
async def test_add_then_clear_lifecycle():
    store = GeometryStore()
    geom_a = {"type": "Point", "coordinates": [0, 0]}
    geom_b = {"type": "Point", "coordinates": [1, 1]}

    # Cycle 1: two alerts.
    await _apply_cycle(
        store,
        [
            {"geometry_ref": "e1:nws:a", "geometry": geom_a},
            {"geometry_ref": "e1:nws:b", "geometry": geom_b},
        ],
        entry_id="e1",
    )
    assert await store.get("e1:nws:a") == geom_a
    assert await store.get("e1:nws:b") == geom_b

    # Cycle 2: only 'a' remains.
    await _apply_cycle(
        store,
        [{"geometry_ref": "e1:nws:a", "geometry": geom_a}],
        entry_id="e1",
    )
    assert await store.get("e1:nws:a") == geom_a
    assert await store.get("e1:nws:b") is None

    # Cycle 3: empty. 'a' purged too.
    await _apply_cycle(store, [], entry_id="e1")
    assert await store.get("e1:nws:a") is None


@pytest.mark.asyncio
async def test_cross_provider_isolation():
    """One entry's empty poll must not wipe another entry's refs."""
    store = GeometryStore()
    geom = {"type": "Point", "coordinates": [0, 0]}

    await _apply_cycle(
        store, [{"geometry_ref": "e_eccc:eccc:x", "geometry": geom}], entry_id="e_eccc"
    )
    # A different entry polls empty — must not touch e_eccc's ref.
    await _apply_cycle(store, [], entry_id="e_nws")
    assert await store.get("e_eccc:eccc:x") == geom


@pytest.mark.asyncio
async def test_same_provider_multi_entry_isolation():
    """Two entries on the SAME provider must not evict each other's geometry.

    Regression: refs were previously ``{provider}:{id}`` and the purge prefix
    was the provider name, so a second NWS entry's poll wiped the first NWS
    entry's polygons (and vice versa) on every cycle.
    """
    store = GeometryStore()
    geom_a = {"type": "Point", "coordinates": [0, 0]}
    geom_b = {"type": "Point", "coordinates": [1, 1]}

    # Entry A (NWS zone 1) sees alert id=1; Entry B (NWS zone 2) sees id=2.
    await _apply_cycle(
        store, [{"geometry_ref": "eA:nws:1", "geometry": geom_a}], entry_id="eA"
    )
    await _apply_cycle(
        store, [{"geometry_ref": "eB:nws:2", "geometry": geom_b}], entry_id="eB"
    )
    # Both survive B's poll.
    assert await store.get("eA:nws:1") == geom_a
    assert await store.get("eB:nws:2") == geom_b

    # A polls again — B's geometry must remain.
    await _apply_cycle(
        store, [{"geometry_ref": "eA:nws:1", "geometry": geom_a}], entry_id="eA"
    )
    assert await store.get("eA:nws:1") == geom_a
    assert await store.get("eB:nws:2") == geom_b


def test_normalize_builds_entry_scoped_geometry_ref(alert_factory):
    """normalize_alerts namespaces geometry_ref by entry_id when geometry exists."""
    from custom_components.cap_alerts.normalize import normalize_alerts

    geom = {"type": "Point", "coordinates": [0, 0]}
    (with_geom,) = normalize_alerts(
        [alert_factory(id="1", provider="nws", geometry=geom)], entry_id="abc123"
    )
    assert with_geom.geometry_ref == "abc123:nws:1"

    # No geometry -> no ref, regardless of entry_id.
    (no_geom,) = normalize_alerts(
        [alert_factory(id="2", provider="nws", geometry=None)], entry_id="abc123"
    )
    assert no_geom.geometry_ref == ""

    # Legacy/default caller (no entry_id) falls back to the unscoped form.
    (legacy,) = normalize_alerts([alert_factory(id="3", provider="nws", geometry=geom)])
    assert legacy.geometry_ref == "nws:3"


@pytest.mark.asyncio
async def test_update_same_ref_overwrites():
    store = GeometryStore()
    g1 = {"type": "Point", "coordinates": [0, 0]}
    g2 = {"type": "Point", "coordinates": [9, 9]}

    await _apply_cycle(
        store, [{"geometry_ref": "e1:nws:a", "geometry": g1}], entry_id="e1"
    )
    await _apply_cycle(
        store, [{"geometry_ref": "e1:nws:a", "geometry": g2}], entry_id="e1"
    )
    assert await store.get("e1:nws:a") == g2
