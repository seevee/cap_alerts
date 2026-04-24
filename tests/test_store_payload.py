"""HA bus event payloads — RFC §2.3 shape."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_homeassistant(monkeypatch):
    """Provide minimal homeassistant stubs so ``store`` imports without HA."""
    if "homeassistant" in sys.modules:
        yield
        return

    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    er_mod = types.ModuleType("homeassistant.helpers.entity_registry")

    class HomeAssistant:  # noqa: D401 — stub
        pass

    core.HomeAssistant = HomeAssistant
    er_mod.async_get = lambda hass: hass.entity_registry

    monkeypatch.setitem(sys.modules, "homeassistant", ha)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er_mod)
    yield


@pytest.fixture
def hass():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    h.entity_registry.async_get_entity_id.return_value = "sensor.cap_alert_test"
    return h


def _fired(hass):
    return [call.args for call in hass.bus.async_fire.call_args_list]


def test_created_fires_with_empty_changed_fields(hass, alert_factory):
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process([alert_factory(id="a", msg_type="Alert")])

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_created"
    assert payload["incident_id"] == "a"
    assert "alert_id" not in payload  # deprecated alias removed
    assert payload["phase_changed"] is True
    assert payload["changed_fields"] == []
    assert payload["entry_id"] == "entry1"
    assert payload["entity_id"] == "sensor.cap_alert_test"


def test_phase_flip_marks_phase_in_changed_fields(hass, alert_factory):
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    first = normalize_alerts([alert_factory(id="a", msg_type="Alert")])
    store.process(first)
    hass.bus.async_fire.reset_mock()

    second = normalize_alerts([alert_factory(id="a", msg_type="Update")])
    store.process(second)

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_updated"
    assert payload["phase_changed"] is True
    assert "phase" in payload["changed_fields"]


def test_headline_change_shows_headline_in_changed_fields(hass, alert_factory):
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    first = normalize_alerts([alert_factory(id="a", headline="first")])
    store.process(first)
    hass.bus.async_fire.reset_mock()

    second = normalize_alerts([alert_factory(id="a", headline="second")])
    store.process(second)

    fired = _fired(hass)
    assert len(fired) == 1
    _, payload = fired[0]
    assert payload["phase_changed"] is False
    assert "headline" in payload["changed_fields"]
    assert "phase" not in payload["changed_fields"]


def test_removed_alert_fires_removed_event(hass, alert_factory):
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(normalize_alerts([alert_factory(id="a", msg_type="Alert")]))
    hass.bus.async_fire.reset_mock()

    # Alert with a future expires disappears → inferred as cancel (provider
    # silently dropped it).
    store.process([])

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_removed"
    assert payload["incident_id"] == "a"
    assert payload["phase"] == "cancel"
    assert payload["changed_fields"] == []


def test_store_fires_removed_with_terminal_phase_cancel(hass, alert_factory):
    """Provider issues an explicit Cancel: removed event carries phase=cancel."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    first = normalize_alerts([alert_factory(id="a", msg_type="Alert")])
    store.process(first)
    hass.bus.async_fire.reset_mock()

    # Second poll: same alert but msg_type=Cancel → phase=cancel.
    cancelled = normalize_alerts([alert_factory(id="a", msg_type="Cancel")])
    result = store.process(cancelled)

    # Cancelled alerts are excluded from the active list.
    assert result == []

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_removed"
    assert payload["incident_id"] == "a"
    assert payload["phase"] == "cancel"
    assert payload["phase_changed"] is True


def test_store_fires_removed_with_terminal_phase_expired(hass, alert_factory):
    """Alert past its expires timestamp drops out as phase=expired."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    first = normalize_alerts([alert_factory(id="a", msg_type="Alert")])
    store.process(first)
    hass.bus.async_fire.reset_mock()

    # Second poll: expires is in the past → normalize tags phase=expired.
    stale = normalize_alerts(
        [alert_factory(id="a", msg_type="Alert", expires="2000-01-01T00:00:00Z")]
    )
    result = store.process(stale)
    assert result == []

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_removed"
    assert payload["phase"] == "expired"
    assert payload["phase_changed"] is True


def test_silent_disappearance_past_expires_inferred_as_expired(hass, alert_factory):
    """Alert drops from the feed without a Cancel and its expires is past."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    # Seed with an alert whose expires is already in the past.
    seeded = normalize_alerts(
        [alert_factory(id="a", msg_type="Alert", expires="2000-01-01T00:00:00Z")]
    )
    # The seeded alert normalizes to phase=expired and never joins the active
    # set; the first process call fires removed. Reset and then feed empty
    # on the second cycle to confirm the previous-map stayed empty.
    store.process(seeded)
    hass.bus.async_fire.reset_mock()

    store.process([])
    # Nothing should fire — the alert was already removed on its first sight.
    assert _fired(hass) == []


def test_silent_disappearance_before_expires_inferred_as_cancel(hass, alert_factory):
    """Alert with a future expires that vanishes is treated as cancel."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    seeded = normalize_alerts(
        [alert_factory(id="a", msg_type="Alert", expires="2099-01-01T00:00:00Z")]
    )
    store.process(seeded)
    hass.bus.async_fire.reset_mock()

    store.process([])

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_removed"
    assert payload["phase"] == "cancel"


def test_first_sight_terminal_alert_fires_removed_only(hass, alert_factory):
    """An alert we've never seen but which is already terminal on arrival."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    cancelled = normalize_alerts([alert_factory(id="a", msg_type="Cancel")])
    result = store.process(cancelled)

    assert result == []
    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_removed"
    assert payload["phase"] == "cancel"
