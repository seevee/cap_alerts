"""One ``incident_removed`` per ending, not one per cycle (issue #145).

A terminal alert is dropped from the active set, so it leaves ``_previous`` and
the next reconciliation reads the same still-published record as a first sighting
that is already terminal. Before the tombstone map that fired a fresh removal
every cycle — once per scan interval while polling, once per ~60 s heartbeat on
ECCC streaming, for as long as the source kept the record (see
``test_coordinator_streaming.py`` for the streaming half).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _entity_registry_from_mock(monkeypatch):
    """Point ``er.async_get`` at the mock ``hass``'s registry attribute.

    The store looks the registry up through ``er.async_get(hass)``, which reads
    ``hass.data``; the fixture below is a MagicMock, so without this the store
    gets a bare mock and the entity id in the payload is a mock too.
    """
    monkeypatch.setattr(
        "custom_components.cap_alerts.store.er.async_get",
        lambda hass: hass.entity_registry,
    )


@pytest.fixture
def hass():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    h.entity_registry.async_get_entity_id.return_value = None
    return h


def _events(hass) -> list[str]:
    return [call.args[0] for call in hass.bus.async_fire.call_args_list]


def _reasons(hass) -> list[str | None]:
    return [
        call.args[1].get("removal_reason")
        for call in hass.bus.async_fire.call_args_list
    ]


def _eccc(alert_factory, **overrides):
    """An ECCC alert, the only shipped source publishing a lifecycle vocabulary."""
    return alert_factory(provider="eccc", msg_type="Update", **overrides)


def test_republished_ended_document_fires_one_removal(hass, alert_factory):
    """The reporter's case: an ECCC watch ends and the record stays in the feed.

    One live poll, then five polls of the same ``ended`` document. On
    0.4.0-alpha.5 this fired ``incident_removed`` five times, each with
    ``phase=cancel`` and ``removal_reason=ended``.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    store.process(normalize_alerts([_eccc(alert_factory, id="a")]))

    ended = normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="ended")])
    for _ in range(5):
        assert store.process(ended) == []

    assert _events(hass) == ["incident_created", "incident_removed"]
    assert _reasons(hass) == [None, "ended"]


def test_terminal_on_first_sight_fires_one_removal(hass, alert_factory):
    """Not an ECCC bug: the clock alone makes the phase terminal.

    An NWS alert that expired in 2020 is terminal on arrival, so it never enters
    the tracked set and every poll used to re-announce the same ending — four
    polls, four removals, no ``incident_created`` at all.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    stale = normalize_alerts(
        [alert_factory(id="a", expires="2020-01-01T00:00:00+00:00")]
    )
    for _ in range(4):
        assert store.process(stale) == []

    assert _events(hass) == ["incident_removed"]


def test_absence_termination_is_not_re_announced_when_the_record_returns(
    hass, alert_factory
):
    """A source that withdraws a record and republishes it terminal.

    Measured behaviour on the NAAD feeds, which drop live *and* ended alerts and
    return them hours later. The absence terminates the alert; the returning
    document must not announce the same ending a second time.
    """
    from custom_components.cap_alerts import store as store_mod
    from custom_components.cap_alerts.conventions import (
        ABSENCE_ENDS,
        SourceConventions,
    )
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(normalize_alerts([alert_factory(id="a")]))

    # Withdrawn from the feed. ABSENCE_ENDS is the one convention under which
    # absence itself terminates; the policy is exercised in test_store_payload.
    with patch.object(
        store_mod,
        "conventions_for",
        return_value=SourceConventions(absence_policy=ABSENCE_ENDS),
    ):
        store.process([])

    # …and back, still terminal.
    returned = normalize_alerts(
        [alert_factory(id="a", expires="2020-01-01T00:00:00+00:00")]
    )
    store.process(returned)

    assert _events(hass) == ["incident_created", "incident_removed"]


def test_an_id_that_comes_back_live_is_created_again(hass, alert_factory):
    """A reissue is news. Swallowing it would be worse than the duplicate."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    live = normalize_alerts([_eccc(alert_factory, id="a")])
    ended = normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="ended")])

    store.process(live)
    store.process(ended)
    store.process(ended)
    result = store.process(live)
    store.process(ended)

    assert [a.id for a in result] == ["a"]
    assert _events(hass) == [
        "incident_created",
        "incident_removed",
        "incident_created",
        "incident_removed",
    ]


def test_a_tombstone_ages_out_after_the_idle_ttl(hass, alert_factory):
    """The tombstone's clock runs on absence, so an aged one stops suppressing.

    The record has not been seen for longer than the store defends an ending, so
    its return is treated as a fresh sighting rather than the same ending
    republished.
    """
    from custom_components.cap_alerts.store import TOMBSTONE_IDLE_TTL, AlertStore
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = AlertStore(hass, "entry1", "eccc")
    ended = normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="ended")])
    store.process(ended)
    assert _events(hass) == ["incident_removed"]

    store._tombstones["a"] = (
        datetime.now(timezone.utc) - TOMBSTONE_IDLE_TTL - timedelta(minutes=1)
    )
    store.process(ended)

    assert _events(hass) == ["incident_removed", "incident_removed"]


def test_suppressing_a_duplicate_refreshes_the_tombstone(hass, alert_factory):
    """Idle ageing: a record that keeps arriving keeps its tombstone alive.

    Without the refresh the TTL would be an absolute age, and a source that
    publishes an ended record for longer than the TTL would resume duplicating
    at the point it elapsed.
    """
    from custom_components.cap_alerts.store import TOMBSTONE_IDLE_TTL, AlertStore
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = AlertStore(hass, "entry1", "eccc")
    ended = normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="ended")])
    store.process(ended)

    # Just inside the window, so this cycle suppresses — and restamps.
    store._tombstones["a"] = (
        datetime.now(timezone.utc) - TOMBSTONE_IDLE_TTL + timedelta(minutes=1)
    )
    store.process(ended)
    assert datetime.now(timezone.utc) - store._tombstones["a"] < timedelta(minutes=1)

    assert _events(hass) == ["incident_removed"]


def test_tombstones_are_pruned_once_the_record_stops_arriving(hass, alert_factory):
    """Bounded by ids terminated recently, not by the life of the entry."""
    from custom_components.cap_alerts.store import TOMBSTONE_IDLE_TTL, AlertStore
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = AlertStore(hass, "entry1", "eccc")
    store.process(
        normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="ended")])
    )
    assert "a" in store._tombstones

    store._tombstones["a"] = (
        datetime.now(timezone.utc) - TOMBSTONE_IDLE_TTL - timedelta(minutes=1)
    )
    store.process([])

    assert store._tombstones == {}
