"""Tests for entity-registry sync logic and identity hashing in sensor.py."""

from __future__ import annotations

import hashlib

from custom_components.cap_alerts import sensor


# --- _short_hash / _alert_object_id ------------------------------------------


def test_short_hash_matches_sha1_prefix():
    uid = "entryA_nws_alert-123"
    assert sensor._short_hash(uid) == hashlib.sha1(uid.encode()).hexdigest()[:8]


def test_object_id_is_collision_proof_across_same_event():
    uid_a = "entry_nws_aaa"
    uid_b = "entry_nws_bbb"
    oid_a = sensor._alert_object_id(uid_a, "Tornado Warning")
    oid_b = sensor._alert_object_id(uid_b, "Tornado Warning")
    assert oid_a != oid_b
    assert oid_a.startswith("cap_alert_tornado_warning_")
    assert oid_b.startswith("cap_alert_tornado_warning_")
    assert len(oid_a.rsplit("_", 1)[1]) == 8


def test_object_id_stable_for_same_unique_id():
    uid = "entry_nws_xyz"
    assert sensor._alert_object_id(uid, "Heat Advisory") == sensor._alert_object_id(
        uid, "Heat Advisory"
    )


# --- _classify_sync ----------------------------------------------------------


def test_classify_add_only():
    to_add, to_remove = sensor._classify_sync({"a", "b"}, set(), set())
    assert to_add == {"a", "b"}
    assert to_remove == set()


def test_classify_remove_only():
    to_add, to_remove = sensor._classify_sync(set(), {"a", "b"}, set())
    assert to_add == set()
    assert to_remove == {"a", "b"}


def test_classify_mixed_add_and_remove():
    to_add, to_remove = sensor._classify_sync({"a", "c"}, {"a", "b"}, set())
    assert to_add == {"c"}
    assert to_remove == {"b"}


def test_classify_fanout_50():
    current = {f"n{i}" for i in range(50)}
    to_add, to_remove = sensor._classify_sync(current, set(), set())
    assert to_add == current
    assert to_remove == set()

    to_add2, to_remove2 = sensor._classify_sync(set(), current, set())
    assert to_add2 == set()
    assert to_remove2 == current


def test_classify_grace_exempts_hydrated_ids_from_removal():
    # Startup: 3 hydrated alerts, coordinator returns empty. No removals.
    to_add, to_remove = sensor._classify_sync(
        current_ids=set(),
        tracked_ids={"a", "b", "c"},
        grace_ids={"a", "b", "c"},
    )
    assert to_add == set()
    assert to_remove == set()


def test_classify_grace_cleared_yields_normal_removal():
    # Second poll with grace cleared: all 3 removed.
    to_add, to_remove = sensor._classify_sync(
        current_ids=set(),
        tracked_ids={"a", "b", "c"},
        grace_ids=set(),
    )
    assert to_remove == {"a", "b", "c"}


def test_classify_partial_grace_still_removes_non_grace_ids():
    # Grace only protects hydrated IDs; a newly-tracked ID should still be removed.
    to_add, to_remove = sensor._classify_sync(
        current_ids=set(),
        tracked_ids={"a", "b", "new"},
        grace_ids={"a", "b"},
    )
    assert to_remove == {"new"}


# --- simulated idempotent remove --------------------------------------------


class _FakeEntReg:
    def __init__(self, entity_ids):
        self._entities = set(entity_ids)
        self.removed: list[str] = []

    def async_get(self, entity_id):
        return object() if entity_id in self._entities else None

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self._entities.discard(entity_id)


def test_idempotent_remove_skips_missing_registry_entries():
    # Simulate the removal path: ent_reg.async_get gate prevents double-remove.
    ent_reg = _FakeEntReg(["sensor.cap_alert_a"])
    for eid in ["sensor.cap_alert_a", "sensor.cap_alert_gone"]:
        if ent_reg.async_get(eid):
            ent_reg.async_remove(eid)
    assert ent_reg.removed == ["sensor.cap_alert_a"]


# --- restart-grace scenario (integration of _classify_sync + state flip) ----


def test_restart_grace_two_cycle_sequence():
    """Hydrate 3; first poll empty → no removals; second poll empty → all removed."""
    tracked = {"a", "b", "c"}
    grace = {"a", "b", "c"}
    first_sync = True

    # First cycle
    active = grace if first_sync else set()
    _, to_remove = sensor._classify_sync(set(), tracked, active)
    assert to_remove == set()
    if first_sync:
        grace.clear()
        first_sync = False

    # Second cycle (grace cleared)
    active = grace if first_sync else set()
    _, to_remove = sensor._classify_sync(set(), tracked, active)
    assert to_remove == {"a", "b", "c"}


# --- count sensor breakdown --------------------------------------------------


def test_count_sensor_state_stays_the_total_with_a_breakdown_alongside(alert_factory):
    """State is every alert; the active/upcoming split rides as attributes (#99).

    The split itself is covered in test_count_breakdown.py; what this pins is
    that changing the state's meaning is off the table — templates already key
    off the total.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    soon = (now + timedelta(hours=6)).isoformat()
    earlier = (now - timedelta(hours=6)).isoformat()

    class FakeCoord:
        data = {
            "a": alert_factory(id="a", onset=earlier),
            "b": alert_factory(id="b", onset=soon),
            "c": alert_factory(id="c", onset=soon),
        }

    class FakeSelf:
        coordinator = FakeCoord()

    assert sensor.CountSensor.native_value.fget(FakeSelf) == 3
    assert sensor.CountSensor.extra_state_attributes.fget(FakeSelf) == {
        "active": 1,
        "upcoming": 2,
    }


def test_count_sensor_breakdown_with_no_data():
    class FakeCoord:
        data = None

    class FakeSelf:
        coordinator = FakeCoord()

    assert sensor.CountSensor.native_value.fget(FakeSelf) == 0
    assert sensor.CountSensor.extra_state_attributes.fget(FakeSelf) == {
        "active": 0,
        "upcoming": 0,
    }


# --- device_info.name decoupling --------------------------------------------


def test_sensor_device_info_delegates_to_the_coordinator():
    """Neither sensor entity re-derives device identity.

    device.name must stay stable and derive from the provider, not entry.title:
    HA composes entity_id slugs from device.name + entity.name at first
    registration, so a title carrying lat/long or zone codes would bake volatile
    reconfigure data into them. The derivation itself lives on the coordinator —
    one source of truth for all three platforms, asserted against the real device
    registry in test_refresh_button.py. What this pins is that the sensors defer
    to it rather than keeping a second copy that could drift.
    """
    sentinel = {"identifiers": {("cap_alerts", "01KP7B41CFK72KRHSG16DBJ1E1")}}

    class FakeCoord:
        device_info = sentinel

    class FakeSelf:
        coordinator = FakeCoord()

    assert sensor._CAPAlertsEntity.device_info.fget(FakeSelf) is sentinel
    assert sensor.AlertEntity.device_info.fget(FakeSelf) is sentinel
