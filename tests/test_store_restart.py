"""A restart re-validates known alerts instead of re-announcing them (issue #250).

The store is in-memory, so after a boot ``_previous`` is empty. The entity
registry is not, and the ids under the entry's prefix are the set known before
the restart. The store seeds itself from that set so the first reconciliation
fires only what actually changed while HA was down.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.cap_alerts.const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)
from custom_components.cap_alerts.coordinator import known_alert_entities
from custom_components.cap_alerts.store import AlertStore


@pytest.fixture
def hass():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    h.entity_registry.async_get_entity_id.return_value = None
    return h


@pytest.fixture
def registry(monkeypatch, hass):
    """Registry entries for this entry; tests append unique_ids before building."""
    entries: list[SimpleNamespace] = []
    monkeypatch.setattr(
        "custom_components.cap_alerts.coordinator.er.async_get",
        lambda h: hass.entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.cap_alerts.coordinator.er.async_entries_for_config_entry",
        lambda reg, entry_id: entries,
    )
    return entries


def _store(hass, provider: str) -> AlertStore:
    """A store seeded from the registry the way the coordinator seeds it."""
    return AlertStore(
        "entry1",
        provider,
        fire=hass.bus.async_fire,
        lookup_entity_id=lambda alert_id: hass.entity_registry.async_get_entity_id(
            "sensor", "cap_alerts", f"entry1_{provider}_{alert_id}"
        ),
        known_at_boot=known_alert_entities(hass, "entry1", provider),
    )


def _known(registry, *alert_ids: str) -> None:
    """Registry rows as HA keeps them: unique_id plus the name at registration."""
    registry.append(SimpleNamespace(unique_id="entry1_count", original_name="Count"))
    registry.extend(
        SimpleNamespace(
            unique_id=f"entry1_nws_{alert_id}", original_name=f"Event {alert_id}"
        )
        for alert_id in alert_ids
    )


def _fired(hass) -> list[tuple[str, str]]:
    return [
        (call.args[0], call.args[1]["incident_id"])
        for call in hass.bus.async_fire.call_args_list
    ]


def test_known_live_alert_is_revalidated_silently(hass, registry, alert_factory):
    _known(registry, "a")
    store = _store(hass, "nws")

    result = store.process([alert_factory(id="a", phase="update")])

    assert [a.id for a in result] == ["a"]
    assert result[0].phase_changed is False
    assert _fired(hass) == []


def test_alert_issued_during_downtime_is_created(hass, registry, alert_factory):
    _known(registry, "a")
    store = _store(hass, "nws")

    store.process(
        [alert_factory(id="a", phase="update"), alert_factory(id="b", phase="new")]
    )

    assert _fired(hass) == [(EVENT_INCIDENT_CREATED, "b")]


def test_alert_ended_during_downtime_is_removed_after_grace(
    hass, registry, alert_factory
):
    """Absent on the first reconciliation is the sensor's grace; the second ends it."""
    _known(registry, "gone")
    # The entity is still registered at this point: the sensor drops it on the
    # same reconciliation, after this event fires.
    hass.entity_registry.async_get_entity_id.return_value = "sensor.cap_alert_gone"
    store = _store(hass, "nws")

    store.process([])
    assert _fired(hass) == []

    store.process([])
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "gone")]
    payload = hass.bus.async_fire.call_args.args[1]
    assert payload["phase"] == "cancel"
    assert payload["severity"] == "unknown"
    assert payload["event"] == "Event gone"
    assert payload["area_desc"] == ""
    assert payload["entity_id"] == "sensor.cap_alert_gone"
    assert "removal_reason" not in payload

    store.process([])
    assert len(_fired(hass)) == 1


def test_stream_rebuilds_do_not_spend_the_grace(hass, registry, alert_factory):
    """Only a fetch can say a known alert is gone (issue #252).

    On ECCC streaming the second reconciliation is a heartbeat a minute after
    boot, and the backfill that seeded the store drops live alerts. Counting it
    would announce an alert still in force as ended.
    """
    _known(registry, "gone")
    store = _store(hass, "nws")

    store.process([])
    for _ in range(3):
        store.process([], fetched=False)
    assert _fired(hass) == []
    assert store.boot_pending == {"gone"}

    store.process([])
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "gone")]
    assert store.boot_pending == frozenset()


def test_stream_sighting_settles_a_known_alert(hass, registry, alert_factory):
    """A streamed document is still a sighting, fetch or not."""
    _known(registry, "a", "b")
    store = _store(hass, "nws")

    store.process([])
    store.process([alert_factory(id="a", phase="update")], fetched=False)

    assert store.boot_pending == {"b"}
    assert _fired(hass) == []


def test_known_alert_back_within_grace_is_not_news(hass, registry, alert_factory):
    _known(registry, "a")
    store = _store(hass, "nws")

    store.process([])
    store.process([alert_factory(id="a", phase="update")])

    assert _fired(hass) == []


def test_known_alert_terminal_at_boot_is_removed_once(hass, registry, alert_factory):
    _known(registry, "a")
    store = _store(hass, "nws")

    store.process([alert_factory(id="a", phase="cancel")])
    store.process([])

    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]


def test_other_providers_ids_are_not_known(hass, registry, alert_factory):
    registry.append(SimpleNamespace(unique_id="entry1_eccc_a", original_name="A"))
    store = _store(hass, "nws")

    store.process([alert_factory(id="a", phase="new")])

    assert _fired(hass) == [(EVENT_INCIDENT_CREATED, "a")]


def test_announced_ending_is_not_re_announced_after_restart(
    hass, registry, alert_factory
):
    """The tombstone died with the old store, and the entity with the ending (#257).

    ECCC keeps an ended record in the feed for up to 48 h, so a restart inside
    that window used to read it as a first sighting already terminal.
    """
    before = _store(hass, "nws")
    before.process([alert_factory(id="a", phase="new")])
    before.process([alert_factory(id="a", phase="cancel")])
    assert _fired(hass) == [
        (EVENT_INCIDENT_CREATED, "a"),
        (EVENT_INCIDENT_REMOVED, "a"),
    ]

    after = _store(hass, "nws")
    for _ in range(3):
        after.process([alert_factory(id="a", phase="cancel")])

    assert len(_fired(hass)) == 2


def test_boot_suppression_tombstones_the_lineage(hass, registry, alert_factory):
    """A later revision of the suppressed ending is the same ending (#185)."""
    store = _store(hass, "eccc")
    store.process([alert_factory(id="a", identifier="cap-a", phase="cancel")])
    store.process(
        [
            alert_factory(
                id="b", identifier="cap-b", phase="cancel", references=["cap-a"]
            )
        ]
    )

    assert _fired(hass) == []


def test_stream_rebuilds_before_the_first_fetch_stay_in_boot(
    hass, registry, alert_factory
):
    """Only a fetch ends the boot, as it ends the grace (#252)."""
    store = _store(hass, "nws")
    store.process([alert_factory(id="a", phase="cancel")], fetched=False)
    store.process([alert_factory(id="b", phase="cancel")])
    store.process([alert_factory(id="c", phase="cancel")])

    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "c")]


def test_terminal_after_the_boot_fetch_is_announced(hass, registry, alert_factory):
    """The suppression is the boot fetch's alone; later first sightings fire."""
    store = _store(hass, "nws")
    store.process([])
    store.process([alert_factory(id="a", phase="cancel")])

    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]


# ---------------------------------------------------------------------------
# Restored records (issue #350): the lifecycle fields saved before the restart
# ---------------------------------------------------------------------------

RECORD = {
    "identifier": "urn:oid:A",
    "sender": "",
    "event": "Wind Warning",
    "area_desc": "Ottawa",
    "severity_normalized": "moderate",
    "phase": "new",
    "expires": "2099-01-01T00:00:00+00:00",
}


def _restored(hass, provider: str = "nws", **records) -> AlertStore:
    """A store seeded from the restore file, no registry rows."""
    store = _store(hass, provider)
    store.restore(records, confirmed_at="2026-10-27T15:00:00+00:00")
    return store


def _live(alert_factory, **overrides):
    """The live alert behind ``RECORD``, as a fetch returns it."""
    fields = {
        "id": "a",
        "identifier": "urn:oid:A",
        "area_desc": "Ottawa",
        "severity_normalized": "moderate",
        "phase": "new",
        "headline": "Wind Warning in effect",
        "description": "A description the record never held",
    }
    fields.update(overrides)
    return alert_factory(**fields)


def _payloads(hass, event_type: str) -> list[dict]:
    return [
        call.args[1]
        for call in hass.bus.async_fire.call_args_list
        if call.args[0] == event_type
    ]


def test_restore_seeds_and_counts(hass, registry):
    store = _restored(hass, a=RECORD, b={**RECORD, "identifier": "urn:oid:B"})

    assert store.seeded_at_boot == 2
    assert store.boot_pending == frozenset()


def test_restore_pops_only_its_ids_from_the_registry_grace(hass, registry):
    _known(registry, "a", "r")
    store = _store(hass, "nws")

    store.restore({"a": RECORD}, confirmed_at="t")

    assert store.boot_pending == frozenset({"r"})


def test_restored_live_alert_with_only_content_differing_fires_nothing(
    hass, registry, alert_factory
):
    store = _restored(hass, a=RECORD)

    result = store.process([_live(alert_factory)])

    assert _fired(hass) == []
    (alert,) = result
    assert alert.stale is False
    assert alert.description == "A description the record never held"


def test_restored_record_is_replaced_by_the_live_content(hass, registry, alert_factory):
    """After one sighting the diff is the ordinary one: content changes count."""
    store = _restored(hass, a=RECORD)
    store.process([_live(alert_factory)])

    store.process([_live(alert_factory, headline="Updated headline")])

    (updated,) = _payloads(hass, EVENT_INCIDENT_UPDATED)
    assert updated["changed_fields"] == ["headline"]


def test_restored_severity_change_is_announced(hass, registry, alert_factory):
    store = _restored(hass, a=RECORD)

    store.process([_live(alert_factory, severity_normalized="severe")])

    (updated,) = _payloads(hass, EVENT_INCIDENT_UPDATED)
    assert updated["changed_fields"] == ["severity_normalized"]
    assert updated["previous_severity"] == "moderate"
    assert updated["severity"] == "severe"


def test_restored_absent_alert_is_retained_to_its_expiry(hass, registry, alert_factory):
    store = _restored(hass, a=RECORD)

    result = store.process([])

    assert _fired(hass) == []
    (retained,) = result
    assert retained.id == "a"
    assert retained.stale is True
    assert retained.last_confirmed == "2026-10-27T15:00:00+00:00"
    assert retained.severity_normalized == "moderate"


def test_restored_alert_expired_while_down_is_removed_with_its_fields(
    hass, registry, alert_factory
):
    """On the first reconciliation, not the second: the record knows its expiry."""
    store = _restored(hass, a={**RECORD, "expires": "2026-01-01T00:00:00+00:00"})

    result = store.process([])

    assert result == []
    (removed,) = _payloads(hass, EVENT_INCIDENT_REMOVED)
    assert removed["incident_id"] == "a"
    assert removed["phase"] == "expired"
    assert removed["severity"] == "moderate"
    assert removed["area_desc"] == "Ottawa"
    assert removed["event"] == "Wind Warning"


def test_restored_alert_on_an_absence_ends_source_is_removed_with_its_fields(
    hass, registry, alert_factory
):
    """GDACS ends on absence; no expiry, so the inferred phase is cancel."""
    store = _restored(hass, "gdacs", a={**RECORD, "expires": ""})

    result = store.process([])

    assert result == []
    (removed,) = _payloads(hass, EVENT_INCIDENT_REMOVED)
    assert removed["phase"] == "cancel"
    assert removed["severity"] == "moderate"
    assert removed["area_desc"] == "Ottawa"


def test_restored_alert_terminal_on_the_first_fetch_is_removed_once(
    hass, registry, alert_factory
):
    store = _restored(hass, a=RECORD)
    terminal = _live(alert_factory, phase="cancel")

    store.process([terminal])
    store.process([terminal])

    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]
    (removed,) = _payloads(hass, EVENT_INCIDENT_REMOVED)
    assert removed["severity"] == "moderate"


def test_a_successor_supersedes_a_restored_record(hass, registry, alert_factory):
    """The record keeps ``identifier`` so a new revision updates, not duplicates."""
    store = _restored(hass, a=RECORD)
    successor = _live(
        alert_factory,
        id="a2",
        identifier="urn:oid:A2",
        references=("urn:oid:A",),
        severity_normalized="severe",
    )

    result = store.process([successor])

    assert [a.id for a in result] == ["a2"]
    assert _fired(hass) == [(EVENT_INCIDENT_UPDATED, "a2")]
    (updated,) = _payloads(hass, EVENT_INCIDENT_UPDATED)
    assert updated["changed_fields"] == ["severity_normalized"]
    assert updated["previous_severity"] == "moderate"


def test_a_scope_change_removes_a_restored_absent_alert_with_its_fields(
    hass, registry, alert_factory
):
    """Retention is suspended, so the alert is not stranded; the payload is full."""
    store = _restored(hass, a=RECORD)

    result = store.process([], scope_changed=True)

    assert result == []
    (removed,) = _payloads(hass, EVENT_INCIDENT_REMOVED)
    assert removed["phase"] == "cancel"
    assert removed["severity"] == "moderate"
    assert removed["area_desc"] == "Ottawa"


def test_registry_ids_without_a_record_keep_the_grace(hass, registry, alert_factory):
    """Two paths side by side: the record decides at once, the registry waits."""
    _known(registry, "r")
    store = _store(hass, "nws")
    store.restore(
        {"a": {**RECORD, "expires": "2026-01-01T00:00:00+00:00"}}, confirmed_at="t"
    )

    store.process([])
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]
    store.process([])

    assert _fired(hass) == [
        (EVENT_INCIDENT_REMOVED, "a"),
        (EVENT_INCIDENT_REMOVED, "r"),
    ]
    removed_r = _payloads(hass, EVENT_INCIDENT_REMOVED)[1]
    assert removed_r["severity"] == "unknown"
