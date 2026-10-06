"""The restore file's serialization and its Store wrapper (issue #281).

The round-trip tests pin that every ``CAPAlert`` field but the three excluded
ones survives the trip to disk, and fail when a field is added to the model
without a value here. The Store tests pin the write discipline RFC §1.4
requirement 5 asks for: nothing written while the set is unchanged, nothing
ever written for a quiet entry.
"""

from __future__ import annotations

import logging
from dataclasses import MISSING, fields, replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.cap_alerts.const import (
    DOMAIN,
    RESTORE_SAVE_DELAY,
    RESTORE_STORAGE_VERSION,
)
from custom_components.cap_alerts.model import CAPAlert, geocodes_from
from custom_components.cap_alerts.restore import (
    EXCLUDED_FIELDS,
    AlertRestoreStore,
    RestoredSet,
    alert_from_storage,
    alert_to_storage,
)
from tests.conftest import make_alert

ENTRY_ID = "entry-1"
KEY = f"{DOMAIN}.{ENTRY_ID}"
CONFIRMED = "2026-10-06T12:00:00+00:00"

# One non-default value per field. ``test_every_field_round_trips`` checks the
# key set against the model, so a new field fails there until it is added.
EVERY_FIELD: dict[str, Any] = {
    "id": "urn:oid:2.49.0.1.124.1",
    "url": "https://example.test/alert.cap",
    "identifier": "urn:oid:2.49.0.1.124.1",
    "event": "Severe Thunderstorm Warning",
    "msg_type": "Update",
    "status": "Actual",
    "scope": "Public",
    "category": "Met",
    "urgency": "Immediate",
    "severity": "Severe",
    "certainty": "Observed",
    "response_type": "Shelter",
    "sent": "2026-10-06T10:00:00+00:00",
    "effective": "2026-10-06T10:00:00+00:00",
    "onset": "2026-10-06T10:30:00+00:00",
    "expires": "2026-10-06T18:00:00+00:00",
    "ends": "2026-10-06T20:00:00+00:00",
    "headline": "headline",
    "description": "description",
    "instruction": "take cover",
    "note": "note",
    "web": "https://example.test/",
    "area_desc": "Somewhere",
    "affected_zones": ("ONZ001", "ONZ002"),
    "affected_zone_uris": ("https://example.test/zones/ONZ001",),
    "geocodes": geocodes_from({"UGC": ["ONZ001"], "layer:EC-MSC-SMC:1.0:CLC": ["1"]}),
    "geometry": {"type": "Polygon", "coordinates": []},
    "geometry_ref": f"{ENTRY_ID}:eccc:urn",
    "bbox": (-80.5, 43.0, -79.0, 44.25),
    "points": ((-79.5, 43.5), (-79.25, 43.75)),
    "is_marine": True,
    "event_code_nws": "SVR",
    "event_code_same": "SVR",
    "vtec": ("/O.NEW.KTOR.SV.W.0001.260606T1000Z-260606T1800Z/",),
    "vtec_office": "KTOR",
    "vtec_phenomena": "SV",
    "vtec_significance": "W",
    "vtec_action": "NEW",
    "vtec_tracking": "0001",
    "sender": "sender@example.test",
    "sender_name": "Sender",
    "references": ("sender,urn:old,2026-10-06T09:00:00+00:00",),
    "replaced_by": "urn:new",
    "replaced_at": "2026-10-06T11:00:00+00:00",
    "parent_id": "urn:parent",
    "parameters": {"Alert_Location_Status": "active", "codes": ["a", "b"], "n": 3},
    "event_alt": "Orage violent",
    "headline_alt": "titre",
    "description_alt": "description fr",
    "instruction_alt": "abritez-vous",
    "language": "en-CA",
    "language_alt": "fr-CA",
    "episode_days": ({"date": "2026-10-06", "severity": "Moderate"},),
    "provider": "eccc",
    "severity_normalized": "severe",
    "phase": "update",
    "lifecycle_status": "active",
    "icon": "mdi:weather-lightning",
    "previous_phase": "new",
    "phase_changed": True,
    "stale": True,
    "last_confirmed": "2026-10-06T11:30:00+00:00",
}


def _default(name: str) -> Any:
    (f,) = [f for f in fields(CAPAlert) if f.name == name]
    if f.default_factory is not MISSING:
        return f.default_factory()
    return f.default


def _stripped(alert: CAPAlert) -> CAPAlert:
    return replace(alert, geometry=None, previous_phase="", phase_changed=False)


def _trip(alert: CAPAlert) -> CAPAlert:
    return alert_from_storage(alert_to_storage(alert))


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_every_field_round_trips() -> None:
    names = {f.name for f in fields(CAPAlert)}
    assert set(EVERY_FIELD) == names, "a CAPAlert field has no test value"
    for f in fields(CAPAlert):
        if f.name != "id":
            assert EVERY_FIELD[f.name] != _default(f.name), f.name

    alert = CAPAlert(**EVERY_FIELD)
    stored = alert_to_storage(alert)

    assert set(stored) == names - EXCLUDED_FIELDS
    assert _trip(alert) == _stripped(alert)


def test_geometry_is_not_persisted() -> None:
    alert = make_alert(
        geometry={"type": "Point", "coordinates": [1.0, 2.0]},
        geometry_ref="entry:nws:test-1",
    )

    stored = alert_to_storage(alert)

    assert "geometry" not in stored
    assert stored["geometry_ref"] == "entry:nws:test-1"
    assert alert_from_storage(stored).geometry is None


def test_transition_metadata_is_not_persisted() -> None:
    alert = make_alert(previous_phase="new", phase_changed=True, phase="update")

    stored = alert_to_storage(alert)

    assert "previous_phase" not in stored
    assert "phase_changed" not in stored
    restored = alert_from_storage(stored)
    assert restored.previous_phase == ""
    assert restored.phase_changed is False
    assert restored.phase == "update"


def test_unknown_keys_are_ignored_and_missing_keys_default() -> None:
    restored = alert_from_storage(
        {"id": "a", "headline": "h", "from_the_future": 1, "geometry": {"x": 1}}
    )

    assert restored == CAPAlert(id="a", headline="h")


def test_a_record_without_an_id_raises() -> None:
    with pytest.raises(TypeError):
        alert_from_storage({"headline": "h"})


# ---------------------------------------------------------------------------
# Store wrapper
# ---------------------------------------------------------------------------


def _stored_alert_ids(hass_storage: dict[str, Any]) -> list[str]:
    return [record["id"] for record in hass_storage[KEY]["data"]["alerts"]]


async def _flush(hass: HomeAssistant) -> None:
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RESTORE_SAVE_DELAY + 1)
    )
    await hass.async_block_till_done()


def _seed(hass_storage: dict[str, Any], data: Any) -> None:
    hass_storage[KEY] = {
        "version": RESTORE_STORAGE_VERSION,
        "minor_version": 1,
        "key": KEY,
        "data": data,
    }


@pytest.mark.asyncio
async def test_missing_file_is_an_empty_set_and_writes_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = AlertRestoreStore(hass, ENTRY_ID)

    assert await store.async_load() is None
    assert not store.async_save_if_changed([], scope_key="s", confirmed_at=CONFIRMED)
    await _flush(hass)

    assert KEY not in hass_storage
    assert store.last_saved is None


@pytest.mark.asyncio
async def test_unchanged_payload_does_not_schedule_a_save(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    alerts = [make_alert(id="b"), make_alert(id="a")]
    _seed(
        hass_storage,
        {
            "scope_key": "s",
            "confirmed_at": CONFIRMED,
            "alerts": [alert_to_storage(a) for a in alerts],
        },
    )
    store = AlertRestoreStore(hass, ENTRY_ID)

    restored = await store.async_load()

    assert restored == RestoredSet(
        alerts=tuple(alerts), scope_key="s", confirmed_at=CONFIRMED
    )
    # A later confirmation, same alerts in another order: no write.
    assert not store.async_save_if_changed(
        reversed(alerts), scope_key="s", confirmed_at="2026-10-06T13:00:00+00:00"
    )
    await _flush(hass)
    assert hass_storage[KEY]["data"]["confirmed_at"] == CONFIRMED
    assert store.last_saved is None


@pytest.mark.asyncio
async def test_changed_payload_schedules_one_save(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = AlertRestoreStore(hass, ENTRY_ID)
    assert await store.async_load() is None

    alerts = [make_alert(id="b", geometry={"type": "Point"}), make_alert(id="a")]
    assert store.async_save_if_changed(alerts, scope_key="s", confirmed_at="t1")
    assert not store.async_save_if_changed(alerts, scope_key="s", confirmed_at="t2")
    assert store.last_saved is not None
    assert KEY not in hass_storage

    await _flush(hass)

    data = hass_storage[KEY]["data"]
    assert _stored_alert_ids(hass_storage) == ["a", "b"]
    assert data["confirmed_at"] == "t1"
    assert data["scope_key"] == "s"
    assert "geometry" not in data["alerts"][1]

    # The next change is compared against what was written, not the empty start.
    assert store.async_save_if_changed(alerts[:1], scope_key="s", confirmed_at="t3")
    await _flush(hass)
    assert _stored_alert_ids(hass_storage) == ["b"]


@pytest.mark.asyncio
async def test_a_corrupt_file_loads_as_none_with_one_warning(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(hass_storage, {"confirmed_at": CONFIRMED, "alerts": [{"headline": "x"}]})
    store = AlertRestoreStore(hass, ENTRY_ID)

    with caplog.at_level(logging.WARNING):
        assert await store.async_load() is None

    warnings = [
        r for r in caplog.records if r.name.endswith(".restore") and r.levelno >= 30
    ]
    assert len(warnings) == 1
    assert KEY in warnings[0].getMessage()
    # An unreadable file is still overwritten at shutdown, even with no alerts.
    await store.async_save_now([], scope_key=None, confirmed_at=CONFIRMED)
    assert hass_storage[KEY]["data"]["alerts"] == []


@pytest.mark.asyncio
async def test_save_now_skips_an_empty_set_with_no_file(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = AlertRestoreStore(hass, ENTRY_ID)
    assert await store.async_load() is None

    await store.async_save_now([], scope_key="s", confirmed_at=CONFIRMED)

    assert KEY not in hass_storage
    assert store.last_saved is None


@pytest.mark.asyncio
async def test_save_now_writes_a_fresh_confirmed_at(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    alerts = [make_alert(id="a")]
    _seed(
        hass_storage,
        {
            "scope_key": None,
            "confirmed_at": CONFIRMED,
            "alerts": [alert_to_storage(a) for a in alerts],
        },
    )
    store = AlertRestoreStore(hass, ENTRY_ID)
    assert await store.async_load() is not None

    later = "2026-10-06T14:00:00+00:00"
    await store.async_save_now(alerts, scope_key=None, confirmed_at=later)

    assert hass_storage[KEY]["data"]["confirmed_at"] == later
    assert store.last_saved is not None
    # The set emptied while running: the empty list is written, not skipped.
    await store.async_save_now([], scope_key=None, confirmed_at=later)
    assert hass_storage[KEY]["data"]["alerts"] == []


@pytest.mark.asyncio
async def test_remove_deletes_the_file(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = AlertRestoreStore(hass, ENTRY_ID)
    await store.async_save_now(
        [make_alert(id="a")], scope_key="s", confirmed_at=CONFIRMED
    )
    assert KEY in hass_storage

    await store.async_remove()

    assert KEY not in hass_storage
    # Forgotten, so a quiet shutdown after removal does not recreate it.
    await store.async_save_now([], scope_key="s", confirmed_at=CONFIRMED)
    assert KEY not in hass_storage
