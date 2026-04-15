"""HA bus event payloads — RFC §2.2.2 shape."""

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
    assert event_type == "cap_alert_created"
    assert payload["incident_id"] == "a"
    assert payload["alert_id"] == "a"
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
    assert event_type == "cap_alert_updated"
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
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process([alert_factory(id="a", msg_type="Alert")])
    hass.bus.async_fire.reset_mock()

    store.process([])

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "cap_alert_removed"
    assert payload["incident_id"] == "a"
    assert payload["phase_changed"] is False
    assert payload["changed_fields"] == []
