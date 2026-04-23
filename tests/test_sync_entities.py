"""Tests for entity-registry sync logic and identity hashing in sensor.py."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import types
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _stub_ha_modules() -> None:
    """Inject minimal stubs for the HA imports used by sensor.py."""
    if "homeassistant" in sys.modules:
        return

    def _mk(name: str, **attrs: object) -> types.ModuleType:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    ha = _mk("homeassistant")
    ha.__path__ = []  # mark as package

    _mk("homeassistant.components")
    sys.modules["homeassistant.components"].__path__ = []
    _mk(
        "homeassistant.components.sensor",
        SensorDeviceClass=type("SensorDeviceClass", (), {"TIMESTAMP": "timestamp"}),
        SensorEntity=type("SensorEntity", (), {}),
        SensorStateClass=type("SensorStateClass", (), {"MEASUREMENT": "measurement"}),
    )
    _mk("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
    _mk(
        "homeassistant.const",
        EntityCategory=type("EntityCategory", (), {"DIAGNOSTIC": "diagnostic"}),
    )
    _mk("homeassistant.core", callback=lambda f: f, HomeAssistant=object)
    _mk("homeassistant.helpers")
    sys.modules["homeassistant.helpers"].__path__ = []
    er_mod = _mk("homeassistant.helpers.entity_registry")
    # Match the surface area that test_store_payload's stub provides, since
    # whichever test loads first wins the namespace.
    er_mod.async_get = lambda hass: getattr(hass, "entity_registry", None)
    er_mod.async_entries_for_config_entry = lambda reg, entry_id: []
    _mk("homeassistant.helpers.device_registry", DeviceInfo=dict)

    coord_mod = _mk("homeassistant.helpers.update_coordinator")
    coord_mod.CoordinatorEntity = type("CoordinatorEntity", (), {})
    # Support CoordinatorEntity[T] generic subscripting
    coord_mod.CoordinatorEntity.__class_getitem__ = classmethod(lambda cls, _i: cls)

    def _slugify(s: str) -> str:
        return "_".join("".join(c.lower() if c.isalnum() else " " for c in s).split())

    _mk("homeassistant.util", slugify=_slugify)

    # Also stub the sibling coordinator module so sensor.py's `.coordinator`
    # import resolves without pulling real HA bits.
    _mk(
        "cap_alerts.coordinator",
        AlertsDataUpdateCoordinator=type("AlertsDataUpdateCoordinator", (), {}),
    )
    sys.modules["custom_components.cap_alerts.coordinator"] = sys.modules[
        "cap_alerts.coordinator"
    ]


def _load_sensor() -> types.ModuleType:
    _stub_ha_modules()
    # Ensure parent stub package exists (conftest.py normally does this).
    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent
    full = "cap_alerts.sensor"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / "sensor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


sensor = _load_sensor()


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
