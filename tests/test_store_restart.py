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
)
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
        "custom_components.cap_alerts.store.er.async_get",
        lambda h: hass.entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.cap_alerts.store.er.async_entries_for_config_entry",
        lambda reg, entry_id: entries,
    )
    return entries


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
    store = AlertStore(hass, "entry1", "nws")

    result = store.process([alert_factory(id="a", phase="update")])

    assert [a.id for a in result] == ["a"]
    assert result[0].phase_changed is False
    assert _fired(hass) == []


def test_alert_issued_during_downtime_is_created(hass, registry, alert_factory):
    _known(registry, "a")
    store = AlertStore(hass, "entry1", "nws")

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
    store = AlertStore(hass, "entry1", "nws")

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
    store = AlertStore(hass, "entry1", "nws")

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
    store = AlertStore(hass, "entry1", "nws")

    store.process([])
    store.process([alert_factory(id="a", phase="update")], fetched=False)

    assert store.boot_pending == {"b"}
    assert _fired(hass) == []


def test_known_alert_back_within_grace_is_not_news(hass, registry, alert_factory):
    _known(registry, "a")
    store = AlertStore(hass, "entry1", "nws")

    store.process([])
    store.process([alert_factory(id="a", phase="update")])

    assert _fired(hass) == []


def test_known_alert_terminal_at_boot_is_removed_once(hass, registry, alert_factory):
    _known(registry, "a")
    store = AlertStore(hass, "entry1", "nws")

    store.process([alert_factory(id="a", phase="cancel")])
    store.process([])

    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]


def test_other_providers_ids_are_not_known(hass, registry, alert_factory):
    registry.append(SimpleNamespace(unique_id="entry1_eccc_a", original_name="A"))
    store = AlertStore(hass, "entry1", "nws")

    store.process([alert_factory(id="a", phase="new")])

    assert _fired(hass) == [(EVENT_INCIDENT_CREATED, "a")]
