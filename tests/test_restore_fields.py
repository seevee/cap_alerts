"""The restore file: what a record holds and how the file behaves (issue #350).

``test_store_restart.py`` pins what the store does with a seeded record and
``test_coordinator_restore.py`` the wiring on a real instance. These pin the
two pieces underneath: the record codec in ``store`` and the ``Store`` wrapper
in ``restore``, over the ``hass_storage`` fixture that stands in for
``.storage/``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.cap_alerts.const import RESTORE_SAVE_DELAY
from custom_components.cap_alerts.model import CAPAlert
from custom_components.cap_alerts.restore import AlertRestoreStore
from custom_components.cap_alerts.store import (
    RECORD_FIELDS,
    RESTORED_DIFF_FIELDS,
    alert_from_record,
    record_from_alert,
)

KEY = "cap_alerts.entry1"


def _record(**overrides: str) -> dict[str, str]:
    record = {
        "identifier": "urn:oid:1",
        "sender": "CWTO",
        "event": "Wind Warning",
        "area_desc": "Ottawa",
        "severity_normalized": "moderate",
        "phase": "new",
        "expires": "2099-01-01T00:00:00+00:00",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_a_record_holds_the_lifecycle_fields_and_nothing_else(alert_factory):
    alert = alert_factory(
        id="a",
        event="Wind Warning",
        identifier="urn:oid:1",
        sender="CWTO",
        area_desc="Ottawa",
        severity_normalized="moderate",
        phase="new",
        headline="Wind Warning in effect",
        description="A long description nobody needs on disk",
        geometry={"type": "Polygon", "coordinates": []},
    )

    record = record_from_alert(alert)

    assert set(record) == set(RECORD_FIELDS)
    assert record == _record()
    assert "headline" not in record
    assert "description" not in record
    assert "geometry" not in record


def test_a_record_round_trips_and_carries_the_provider():
    alert = alert_from_record("a", "eccc", _record())

    assert alert == CAPAlert(id="a", provider="eccc", **_record())
    assert record_from_alert(alert) == _record()


def test_a_record_from_another_version_still_loads():
    """Unknown keys are ignored, missing ones take the model's default."""
    record = _record(headline="content a later version might add")
    del record["sender"]

    alert = alert_from_record("a", "eccc", record)

    assert alert.headline == ""
    assert alert.sender == ""
    assert alert.event == "Wind Warning"


def test_a_first_sighting_diffs_only_the_persisted_fields():
    """The allowlist less the content a record never had."""
    assert RESTORED_DIFF_FIELDS == (
        "severity_normalized",
        "phase",
        "expires",
        "area_desc",
    )


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


async def _flush(hass) -> None:
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RESTORE_SAVE_DELAY + 1)
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_no_file_loads_as_none(hass, hass_storage):
    assert await AlertRestoreStore(hass, "entry1").async_load() is None
    assert KEY not in hass_storage


@pytest.mark.asyncio
async def test_a_changed_set_is_written_after_the_delay(hass, hass_storage):
    store = AlertRestoreStore(hass, "entry1")
    records = {"a": _record()}

    assert store.async_save_if_changed(records, scope_key="s", confirmed_at="t1")
    assert KEY not in hass_storage, "the write is coalesced, not immediate"
    await _flush(hass)

    assert hass_storage[KEY]["data"] == {
        "scope_key": "s",
        "confirmed_at": "t1",
        "alerts": records,
    }
    assert store.last_saved is not None


@pytest.mark.asyncio
async def test_an_unchanged_set_schedules_no_write(hass, hass_storage):
    store = AlertRestoreStore(hass, "entry1")
    records = {"a": _record()}
    store.async_save_if_changed(records, scope_key="s", confirmed_at="t1")
    await _flush(hass)

    # A fresh ``confirmed_at`` alone is not a change.
    assert not store.async_save_if_changed(
        dict(records), scope_key="s", confirmed_at="t2"
    )
    await _flush(hass)

    assert hass_storage[KEY]["data"]["confirmed_at"] == "t1"


@pytest.mark.asyncio
async def test_save_now_writes_immediately_with_the_fresh_confirmed_at(
    hass, hass_storage
):
    store = AlertRestoreStore(hass, "entry1")
    records = {"a": _record()}

    await store.async_save_now(records, scope_key="s", confirmed_at="t2")

    assert hass_storage[KEY]["data"]["confirmed_at"] == "t2"
    assert hass_storage[KEY]["data"]["alerts"] == records


@pytest.mark.asyncio
async def test_save_now_on_a_quiet_entry_creates_no_file(hass, hass_storage):
    await AlertRestoreStore(hass, "entry1").async_save_now(
        {}, scope_key="s", confirmed_at="t1"
    )

    assert KEY not in hass_storage


@pytest.mark.asyncio
async def test_save_now_on_an_emptied_set_rewrites_an_existing_file(hass, hass_storage):
    store = AlertRestoreStore(hass, "entry1")
    await store.async_save_now({"a": _record()}, scope_key="s", confirmed_at="t1")

    await store.async_save_now({}, scope_key="s", confirmed_at="t2")

    assert hass_storage[KEY]["data"]["alerts"] == {}


@pytest.mark.asyncio
async def test_a_saved_file_loads_back(hass, hass_storage):
    records = {"a": _record(), "b": _record(identifier="urn:oid:2")}
    await AlertRestoreStore(hass, "entry1").async_save_now(
        records, scope_key="s", confirmed_at="t1"
    )

    loaded = await AlertRestoreStore(hass, "entry1").async_load()

    assert loaded is not None
    assert loaded.records == records
    assert loaded.scope_key == "s"
    assert loaded.confirmed_at == "t1"


@pytest.mark.asyncio
async def test_loading_marks_the_set_as_saved(hass, hass_storage):
    """The same set after a load is not a change, so a restart costs no write."""
    records = {"a": _record()}
    await AlertRestoreStore(hass, "entry1").async_save_now(
        records, scope_key="s", confirmed_at="t1"
    )
    store = AlertRestoreStore(hass, "entry1")
    await store.async_load()

    assert not store.async_save_if_changed(records, scope_key="s", confirmed_at="t2")


@pytest.mark.asyncio
async def test_remove_deletes_the_file(hass, hass_storage):
    store = AlertRestoreStore(hass, "entry1")
    await store.async_save_now({"a": _record()}, scope_key="s", confirmed_at="t1")

    await store.async_remove()

    assert KEY not in hass_storage
    assert await AlertRestoreStore(hass, "entry1").async_load() is None


@pytest.mark.parametrize(
    "data",
    [
        {"alerts": "junk", "confirmed_at": "t1"},
        {"alerts": {"a": "not a record"}, "confirmed_at": "t1"},
        {"alerts": {"a": {}}},
        [],
    ],
    ids=["alerts-not-a-mapping", "record-not-a-mapping", "no-confirmed-at", "list"],
)
@pytest.mark.asyncio
async def test_an_unreadable_file_counts_as_absent(hass, hass_storage, caplog, data):
    hass_storage[KEY] = {"version": 1, "minor_version": 1, "key": KEY, "data": data}

    assert await AlertRestoreStore(hass, "entry1").async_load() is None

    assert "Discarding unreadable alert restore file" in caplog.text


@pytest.mark.asyncio
async def test_a_newer_file_counts_as_absent(hass, hass_storage, caplog):
    """A version this code has no migration for is a restore lost, not an error."""
    hass_storage[KEY] = {
        "version": 2,
        "minor_version": 1,
        "key": KEY,
        "data": {"alerts": {}, "confirmed_at": "t1"},
    }

    assert await AlertRestoreStore(hass, "entry1").async_load() is None

    assert "Discarding unreadable alert restore file" in caplog.text
