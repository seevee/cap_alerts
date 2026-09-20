"""Cross-poll supersession via CAP <references>."""

from __future__ import annotations

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


def test_terminal_successor_via_references_removes_the_predecessor(hass, alert_factory):
    """A Cancel revision under a new id retires the alert it references.

    The BBK shape: a MoWaS all-clear is a ``Cancel`` document with its own
    identifier (``…-001``) naming the warning (``…-000``) in ``references``.
    One ``incident_removed`` fires, phase ``cancel``, and nothing stays active;
    the predecessor's disappearance is not announced a second time.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "bbk")
    warning = alert_factory(
        id="rev0",
        identifier="mow.DE-SL-SB-SE035-20260920-35-000",
        msg_type="Alert",
        provider="bbk",
        expires="",
        references=(),
    )
    store.process(normalize_alerts([warning]))
    hass.bus.async_fire.reset_mock()

    all_clear = alert_factory(
        id="rev1",
        identifier="mow.DE-SL-SB-SE035-20260920-35-001",
        msg_type="Cancel",
        provider="bbk",
        expires="2099-01-01T00:00:00+00:00",
        references=("mow.DE-SL-SB-SE035-20260920-35-000",),
    )
    result = store.process(normalize_alerts([all_clear]))

    assert result == []
    fired = _fired(hass)
    assert [event_type for event_type, _ in fired] == ["incident_removed"]
    payload = fired[0][1]
    assert payload["incident_id"] == "rev1"
    assert payload["phase"] == "cancel"
    assert payload["phase_changed"] is True
    # BBK publishes no lifecycle vocabulary, so there is no removal_reason to
    # attach: the ``Cancel`` msgType is the whole signal.
    assert "removal_reason" not in payload

    # The next poll lists neither revision: the ending is not re-announced.
    hass.bus.async_fire.reset_mock()
    assert store.process([]) == []
    assert _fired(hass) == []


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
