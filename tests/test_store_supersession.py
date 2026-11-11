"""Cross-poll supersession via CAP <references>."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.cap_alerts.store import AlertStore


@pytest.fixture
def hass():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    h.entity_registry.async_get_entity_id.return_value = None
    return h


def _store(hass, provider: str, **kwargs) -> AlertStore:
    """A store over the mock's bus and registry, wired the way the coordinator does it."""
    return AlertStore(
        "entry1",
        provider,
        fire=hass.bus.async_fire,
        lookup_entity_id=lambda alert_id: hass.entity_registry.async_get_entity_id(
            "sensor", "cap_alerts", f"entry1_{provider}_{alert_id}"
        ),
        **kwargs,
    )


def _fired(hass):
    return [call.args for call in hass.bus.async_fire.call_args_list]


def test_supersession_via_references_does_not_fire_removed(hass, alert_factory):
    """NEW in poll N referenced by UPDATE (different id) in poll N+1 → incident_updated only."""
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = _store(hass, "eccc")

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

    store = _store(hass, "bbk")
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

    store = _store(hass, "eccc")

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

    store = _store(hass, "eccc")

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


def test_supersession_carries_the_predecessors_severity(hass, alert_factory):
    """The incident_updated for B names A's severity as previous (issue #280)."""
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = _store(hass, "eccc")
    alert_a = alert_factory(
        id="K1",
        identifier="id-A",
        msg_type="Alert",
        provider="eccc",
        severity="Moderate",
        expires="2099-01-01T00:00:00+00:00",
    )
    store.process(normalize_alerts([alert_a]))
    hass.bus.async_fire.reset_mock()

    alert_b = alert_factory(
        id="K2",
        identifier="id-B",
        msg_type="Update",
        provider="eccc",
        severity="Extreme",
        expires="2099-01-01T00:00:00+00:00",
        references=("id-A",),
    )
    store.process(normalize_alerts([alert_b]))

    ((event_type, payload),) = _fired(hass)
    assert event_type == "incident_updated"
    assert payload["severity"] == "extreme"
    assert payload["previous_severity"] == "moderate"


def _region(
    alert_factory, alert_id, identifier, code, *, references=(), severity="Severe"
):
    """One region's entity of a MeteoAlarm message split per configured region."""
    from custom_components.cap_alerts.model import geocodes_from

    return alert_factory(
        severity=severity,
        id=alert_id,
        identifier=identifier,
        msg_type="Update" if references else "Alert",
        provider="meteoalarm",
        area_desc=f"Region {code}",
        geocodes=geocodes_from({"EMMA_ID": [code]}),
        references=references,
    )


def test_region_dropped_by_an_update_is_announced(hass, alert_factory):
    """An Update covering fewer regions ends the sibling it dropped.

    Region-picker mode turns one message into an entity per region, all under
    one identifier. The Update keeps region A (same id) and drops B. B's
    hazard has ended here, so it is removed with an event, not dropped
    silently on the strength of its identifier being referenced.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = _store(hass, "meteoalarm")
    store.process(
        normalize_alerts(
            [
                _region(alert_factory, "eA", "M1", "A"),
                _region(alert_factory, "eB", "M1", "B"),
            ]
        )
    )
    hass.bus.async_fire.reset_mock()

    result = store.process(
        normalize_alerts([_region(alert_factory, "eA", "M2", "A", references=("M1",))])
    )

    assert [a.id for a in result] == ["eA"]
    removed = [p for t, p in _fired(hass) if t == "incident_removed"]
    assert [p["incident_id"] for p in removed] == ["eB"]
    assert removed[0]["phase"] == "cancel"


def test_new_region_ids_pair_with_their_own_predecessor(hass, alert_factory):
    """Each new per-region alert updates the sibling over the same region.

    Pairing on identifier alone hands both successors the same predecessor:
    the diff is then computed against the wrong region's record.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = _store(hass, "meteoalarm")
    store.process(
        normalize_alerts(
            [
                _region(alert_factory, "eA", "M1", "A", severity="Severe"),
                _region(alert_factory, "eB", "M1", "B", severity="Moderate"),
            ]
        )
    )
    hass.bus.async_fire.reset_mock()

    store.process(
        normalize_alerts(
            [
                _region(
                    alert_factory,
                    "eA2",
                    "M2",
                    "A",
                    references=("M1",),
                    severity="Severe",
                ),
                _region(
                    alert_factory,
                    "eB2",
                    "M2",
                    "B",
                    references=("M1",),
                    severity="Moderate",
                ),
            ]
        )
    )

    fired = _fired(hass)
    assert [t for t, _ in fired] == ["incident_updated", "incident_updated"]
    previous = {p["incident_id"]: p.get("previous_severity") for _, p in fired}
    assert previous == {"eA2": "severe", "eB2": "moderate"}


def test_update_naming_several_predecessors_ends_none_of_them(hass, alert_factory):
    """AEMET names two earlier messages for the same zone in one Update.

    The hazard carries on in the successor, so neither predecessor is
    announced as ended: one is taken over, the other is covered.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = _store(hass, "meteoalarm")
    store.process(
        normalize_alerts(
            [
                _region(alert_factory, "p1", "PRP1", "Z"),
                _region(alert_factory, "p2", "PRP2", "Z"),
            ]
        )
    )
    hass.bus.async_fire.reset_mock()

    result = store.process(
        normalize_alerts(
            [_region(alert_factory, "s", "S", "Z", references=("PRP1", "PRP2"))]
        )
    )

    assert [a.id for a in result] == ["s"]
    assert [t for t, _ in _fired(hass)] == ["incident_updated"]


def test_revision_moved_off_the_user_is_announced(hass, alert_factory):
    """A revision the caller filtered out still ends the alert it replaces.

    ECCC's ingest reports such revisions through ``superseded_identifiers``.
    Nothing here carries the hazard on, so the predecessor is removed with an
    event rather than vanishing.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts

    store = _store(hass, "eccc")
    store.process(
        normalize_alerts([alert_factory(id="k1", identifier="id-A", provider="eccc")])
    )
    hass.bus.async_fire.reset_mock()

    result = store.process([], superseded_identifiers=frozenset({"id-A"}))

    assert result == []
    fired = _fired(hass)
    assert [t for t, _ in fired] == ["incident_removed"]
    assert fired[0][1]["incident_id"] == "k1"
