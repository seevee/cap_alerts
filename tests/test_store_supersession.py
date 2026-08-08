"""Cross-poll supersession via CAP <references>."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

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

    class HomeAssistant:
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
    h.entity_registry.async_get_entity_id.return_value = None
    return h


def _fired(hass):
    return [call.args for call in hass.bus.async_fire.call_args_list]


def test_supersession_via_references_does_not_fire_removed(hass, alert_factory):
    """NEW in poll N referenced by UPDATE (different id) in poll N+1 → incident_updated only."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")

    # Poll N: alert A — NEW, bilingual key K1, CAP identifier id-A
    alert_a = alert_factory(
        id="bilingual-key-K1",
        identifier="cap-identifier-id-A",
        msg_type="Alert",
        provider="eccc",
        expires="2099-01-01T00:00:00+00:00",
        references=(),
    )
    poll_n = normalize_alerts([alert_a])
    store.process(poll_n)
    hass.bus.async_fire.reset_mock()

    # Poll N+1: alert B — UPDATE, bilingual key K2, references cap-identifier-id-A
    alert_b = alert_factory(
        id="bilingual-key-K2",
        identifier="cap-identifier-id-B",
        msg_type="Update",
        provider="eccc",
        expires="2099-01-01T00:00:00+00:00",
        references=("cap-identifier-id-A",),
    )
    poll_n1 = normalize_alerts([alert_b])
    result = store.process(poll_n1)

    fired = _fired(hass)
    event_types = [event_type for event_type, _ in fired]

    # Exactly one event: incident_updated for B
    assert len(fired) == 1, f"Expected 1 event, got {len(fired)}: {event_types}"
    event_type, payload = fired[0]
    assert event_type == "incident_updated"
    assert payload["incident_id"] == "bilingual-key-K2"

    # No incident_removed for A
    assert "incident_removed" not in event_types

    # B is in the active set
    assert len(result) == 1
    assert result[0].id == "bilingual-key-K2"


def test_no_supersession_when_identifier_not_referenced(hass, alert_factory):
    """Silent disappearance without reference match fires incident_removed normally."""
    from custom_components.cap_alerts import store as store_mod
    from custom_components.cap_alerts.conventions import (
        ABSENCE_ENDS,
        SourceConventions,
    )
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")

    # Poll N: alert A. The source declares ABSENCE_ENDS below so this test
    # stays about supersession rather than retention — see the absence-policy
    # tests in test_store_payload.py.
    alert_a = alert_factory(
        id="K1",
        identifier="id-A",
        msg_type="Alert",
        provider="eccc",
    )
    store.process(normalize_alerts([alert_a]))
    hass.bus.async_fire.reset_mock()

    # Poll N+1: unrelated alert B (no references to A)
    alert_b = alert_factory(
        id="K2",
        identifier="id-B",
        msg_type="Alert",
        provider="eccc",
        expires="2099-01-01T00:00:00+00:00",
        references=(),
    )
    with patch.object(
        store_mod,
        "conventions_for",
        return_value=SourceConventions(absence_policy=ABSENCE_ENDS),
    ):
        store.process(normalize_alerts([alert_b]))

    fired = _fired(hass)
    event_types = [e for e, _ in fired]

    # B created, A removed
    assert "incident_created" in event_types
    assert "incident_removed" in event_types


def test_supersession_previous_phase_carried(hass, alert_factory):
    """The incident_updated event for B carries previous_phase from A."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")

    alert_a = alert_factory(
        id="K1",
        identifier="id-A",
        msg_type="Alert",
        provider="eccc",
        expires="2099-01-01T00:00:00+00:00",
    )
    store.process(normalize_alerts([alert_a]))
    hass.bus.async_fire.reset_mock()

    alert_b = alert_factory(
        id="K2",
        identifier="id-B",
        msg_type="Update",
        provider="eccc",
        expires="2099-01-01T00:00:00+00:00",
        references=("id-A",),
    )
    result = store.process(normalize_alerts([alert_b]))

    assert len(result) == 1
    # previous_phase should be the phase of A (which was "new")
    assert result[0].previous_phase == "new"
