"""``incident_created`` waits for the entity it names (issue #249).

``process()`` runs inside the coordinator's refresh and the sensor platform
adds the alert's entity afterwards, so the registry lookup in ``_fire_event``
missed on every first sighting and the payload never carried ``entity_id``.
With ``defer_until_registered`` on, the store parks those events and the entity
releases them once it exists; what nothing releases goes out at the next
``process()``, after a second look at the registry.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.cap_alerts.const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)
from custom_components.cap_alerts.store import AlertStore


@pytest.fixture(autouse=True)
def _entity_registry_from_mock(monkeypatch):
    monkeypatch.setattr(
        "custom_components.cap_alerts.store.er.async_get",
        lambda hass: hass.entity_registry,
    )


@pytest.fixture
def hass():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    # No entity registered for anything, which is what a first sighting sees.
    h.entity_registry.async_get_entity_id.return_value = None
    return h


def _fired(hass) -> list[tuple[str, str, str | None]]:
    return [
        (call.args[0], call.args[1]["incident_id"], call.args[1].get("entity_id"))
        for call in hass.bus.async_fire.call_args_list
    ]


def _deferring_store(hass) -> AlertStore:
    return AlertStore(hass, "entry1", "nws", defer_until_registered=True)


def test_created_is_parked_until_released_with_the_entity_id(hass, alert_factory):
    store = _deferring_store(hass)

    store.process([alert_factory(id="a")])
    assert _fired(hass) == []

    store.release("a", "sensor.cap_alerts_nws_cap_alert_a_12345678")
    assert _fired(hass) == [
        (EVENT_INCIDENT_CREATED, "a", "sensor.cap_alerts_nws_cap_alert_a_12345678")
    ]


def test_release_is_per_alert_and_fires_each_in_its_own_turn(hass, alert_factory):
    store = _deferring_store(hass)
    store.process([alert_factory(id="a"), alert_factory(id="b")])
    assert _fired(hass) == []

    store.release("b", "sensor.b")
    store.release("a", "sensor.a")
    assert _fired(hass) == [
        (EVENT_INCIDENT_CREATED, "b", "sensor.b"),
        (EVENT_INCIDENT_CREATED, "a", "sensor.a"),
    ]


def test_release_with_nothing_parked_is_a_no_op(hass, alert_factory):
    """Every entity restored from the registry at boot releases nothing."""
    store = _deferring_store(hass)
    store.release("never-seen", "sensor.x")
    assert _fired(hass) == []


def test_cross_poll_supersession_update_is_parked_too(hass, alert_factory):
    """The superseding alert has a new id, so its entity does not exist yet either."""
    store = _deferring_store(hass)
    store.process([alert_factory(id="a", identifier="urn:a")])
    store.release("a", "sensor.a")
    hass.bus.async_fire.reset_mock()

    store.process([alert_factory(id="a2", identifier="urn:a2", references=("urn:a",))])
    assert _fired(hass) == []

    store.release("a2", "sensor.a2")
    assert _fired(hass) == [(EVENT_INCIDENT_UPDATED, "a2", "sensor.a2")]


def test_in_place_update_fires_immediately_when_the_entity_exists(hass, alert_factory):
    store = _deferring_store(hass)
    store.process([alert_factory(id="a")])
    store.release("a", "sensor.a")
    hass.bus.async_fire.reset_mock()

    hass.entity_registry.async_get_entity_id.return_value = "sensor.a"
    store.process([alert_factory(id="a", headline="reworded")])
    assert _fired(hass) == [(EVENT_INCIDENT_UPDATED, "a", "sensor.a")]


def test_removed_is_never_parked(hass, alert_factory):
    """First sighting already terminal: no entity will ever exist for it."""
    store = _deferring_store(hass)
    store.process([alert_factory(id="a", phase="cancel")])
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a", None)]


def test_leftovers_fire_at_the_next_process_ahead_of_that_cycle(hass, alert_factory):
    """No release came; the next reconciliation flushes first, re-checking the registry."""
    store = _deferring_store(hass)
    store.process([alert_factory(id="a")])
    assert _fired(hass) == []

    # HA created the registry entry but never added the entity (disabled, say):
    # the lookup now hits even though ``release`` was never called.
    hass.entity_registry.async_get_entity_id.side_effect = (
        lambda domain, platform, unique_id: {
            "entry1_nws_a": "sensor.a",
            "entry1_nws_b": None,
        }[unique_id]
    )
    store.process([alert_factory(id="a", phase="cancel"), alert_factory(id="b")])
    assert _fired(hass) == [
        (EVENT_INCIDENT_CREATED, "a", "sensor.a"),
        (EVENT_INCIDENT_REMOVED, "a", "sensor.a"),
    ]
    # ``b`` is this cycle's first sighting and waits for its own release.
    store.release("b", "sensor.b")
    assert _fired(hass)[-1] == (EVENT_INCIDENT_CREATED, "b", "sensor.b")


def test_leftover_fires_bare_when_the_registry_never_got_an_entry(hass, alert_factory):
    store = _deferring_store(hass)
    store.process([alert_factory(id="a")])
    store.process([alert_factory(id="a")])
    assert _fired(hass) == [(EVENT_INCIDENT_CREATED, "a", None)]
    # Flushed once, not re-fired on every cycle.
    store.process([alert_factory(id="a")])
    assert len(_fired(hass)) == 1


def test_flag_off_keeps_the_immediate_fire(hass, alert_factory):
    store = AlertStore(hass, "entry1", "nws")
    store.process([alert_factory(id="a")])
    assert _fired(hass) == [(EVENT_INCIDENT_CREATED, "a", None)]
    store.release("a", "sensor.a")
    assert len(_fired(hass)) == 1
