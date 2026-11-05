"""Coordinator writes and purges geometry in lockstep with alert lifecycle."""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.geometry_store import GeometryStore


# Reproduce the geometry put/purge step of _async_update_data in isolation so
# we don't need to spin up HA or the provider layer. Mirrors coordinator.py:
# refs are entry-namespaced, the store content-addresses them on put (#357),
# and the purge is scoped to this entry's prefix. Returns the refs stored.
async def _apply_cycle(store, alerts, entry_id: str) -> list[str]:
    active_refs: set[str] = set()
    stored: list[str] = []
    for a in alerts:
        if a.get("geometry_ref") and a.get("geometry"):
            ref = await store.put_content(a["geometry_ref"], a["geometry"])
            active_refs.add(ref)
            stored.append(ref)
    await store.purge_missing(active_refs, prefix=f"{entry_id}:")
    return stored


@pytest.mark.asyncio
async def test_add_then_clear_lifecycle():
    store = GeometryStore()
    geom_a = {"type": "Point", "coordinates": [0, 0]}
    geom_b = {"type": "Point", "coordinates": [1, 1]}

    # Cycle 1: two alerts.
    ref_a, ref_b = await _apply_cycle(
        store,
        [
            {"geometry_ref": "e1:nws:a", "geometry": geom_a},
            {"geometry_ref": "e1:nws:b", "geometry": geom_b},
        ],
        entry_id="e1",
    )
    assert await store.get(ref_a) == geom_a
    assert await store.get(ref_b) == geom_b

    # Cycle 2: only 'a' remains, under the same ref since its shape held.
    (again,) = await _apply_cycle(
        store,
        [{"geometry_ref": "e1:nws:a", "geometry": geom_a}],
        entry_id="e1",
    )
    assert again == ref_a
    assert await store.get(ref_a) == geom_a
    assert await store.get(ref_b) is None

    # Cycle 3: empty. 'a' purged too.
    await _apply_cycle(store, [], entry_id="e1")
    assert await store.get(ref_a) is None


@pytest.mark.asyncio
async def test_cross_provider_isolation():
    """One entry's empty poll must not wipe another entry's refs."""
    store = GeometryStore()
    geom = {"type": "Point", "coordinates": [0, 0]}

    (ref,) = await _apply_cycle(
        store, [{"geometry_ref": "e_eccc:eccc:x", "geometry": geom}], entry_id="e_eccc"
    )
    # A different entry polls empty — must not touch e_eccc's ref.
    await _apply_cycle(store, [], entry_id="e_nws")
    assert await store.get(ref) == geom


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
    (ref_a,) = await _apply_cycle(
        store, [{"geometry_ref": "eA:nws:1", "geometry": geom_a}], entry_id="eA"
    )
    (ref_b,) = await _apply_cycle(
        store, [{"geometry_ref": "eB:nws:2", "geometry": geom_b}], entry_id="eB"
    )
    # Both survive B's poll.
    assert await store.get(ref_a) == geom_a
    assert await store.get(ref_b) == geom_b

    # A polls again — B's geometry must remain.
    await _apply_cycle(
        store, [{"geometry_ref": "eA:nws:1", "geometry": geom_a}], entry_id="eA"
    )
    assert await store.get(ref_a) == geom_a
    assert await store.get(ref_b) == geom_b


def test_normalize_builds_entry_scoped_geometry_ref(alert_factory):
    """normalize_alerts namespaces the base geometry_ref by entry_id when
    geometry exists; the store adds the shape segment on put (#357)."""
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
async def test_put_content_addresses_the_ref_by_shape():
    """Same base, new polygon, new ref; same polygon under any key order, same
    ref (#357). The card caches a ref as immutable, so a changed shape has
    to be a changed handle."""
    store = GeometryStore()
    issued = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
    }
    trimmed = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    reordered = {"coordinates": issued["coordinates"], "type": "Polygon"}

    first = await store.put_content("e1:nws:svr", issued)
    second = await store.put_content("e1:nws:svr", trimmed)
    same = await store.put_content("e1:nws:svr", reordered)

    base, shape = first.rsplit(":", 1)
    assert base == "e1:nws:svr" and len(shape) == 8 and int(shape, 16) >= 0
    assert first != second
    assert first == same
    assert second.rsplit(":", 1)[0] == "e1:nws:svr"
    # Both shapes are stored side by side until a purge decides.
    assert await store.get(first) == issued
    assert await store.get(second) == trimmed
    assert store.usage("e1")[0] == 2


@pytest.mark.asyncio
async def test_shape_change_mints_new_ref_and_purges_old(alert_factory):
    """A polygon that changes under a stable id leaves the old ref behind in
    the active set, so the cycle's purge drops it (#357)."""
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = GeometryStore()
    issued = {"type": "Point", "coordinates": [0, 0]}
    moved = {"type": "Point", "coordinates": [9, 9]}

    (first,) = normalize_alerts(
        [alert_factory(id="svr", provider="nws", geometry=issued)], entry_id="e1"
    )
    (first_ref,) = await _apply_cycle(
        store,
        [{"geometry_ref": first.geometry_ref, "geometry": first.geometry}],
        entry_id="e1",
    )
    (second,) = normalize_alerts(
        [alert_factory(id="svr", provider="nws", geometry=moved)], entry_id="e1"
    )
    (second_ref,) = await _apply_cycle(
        store,
        [{"geometry_ref": second.geometry_ref, "geometry": second.geometry}],
        entry_id="e1",
    )

    assert first.geometry_ref == second.geometry_ref == "e1:nws:svr"
    assert first_ref != second_ref
    assert await store.get(first_ref) is None
    assert await store.get(second_ref) == moved


@pytest.mark.asyncio
async def test_same_shape_reput_keeps_one_ref():
    """Re-putting an unchanged polygon lands on the same ref and stores it
    once; the content segment only moves when the shape does (#357)."""
    store = GeometryStore()
    g1 = {"type": "Point", "coordinates": [0, 0]}

    (first,) = await _apply_cycle(
        store, [{"geometry_ref": "e1:nws:a", "geometry": g1}], entry_id="e1"
    )
    (second,) = await _apply_cycle(
        store, [{"geometry_ref": "e1:nws:a", "geometry": dict(g1)}], entry_id="e1"
    )
    assert first == second
    assert await store.get(first) == g1
    assert store.usage("e1") == (1, len('{"coordinates":[0,0],"type":"Point"}'))
