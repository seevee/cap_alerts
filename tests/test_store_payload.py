"""HA bus event payloads — RFC §2.3 shape."""

from __future__ import annotations

from datetime import datetime, timezone
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
    from custom_components.cap_alerts import store as store_mod
    from custom_components.cap_alerts.conventions import (
        ABSENCE_ENDS,
        SourceConventions,
    )
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(normalize_alerts([alert_factory(id="a", msg_type="Alert")]))
    hass.bus.async_fire.reset_mock()

    # This test is about removal *mechanics*, not absence policy, so the
    # source declares ABSENCE_ENDS — the one convention under which absence
    # itself terminates. The policy is exercised by the absence tests below.
    with patch.object(
        store_mod,
        "conventions_for",
        return_value=SourceConventions(absence_policy=ABSENCE_ENDS),
    ):
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


def test_absence_within_expires_retains_the_alert(hass, alert_factory):
    """One missed reconciliation is not a lifecycle signal (RFC §1.4 item 8).

    The alert stays in the active set, marked stale, and nothing fires: a feed
    gap must not clear a live hazard from the dashboard, and must not re-create
    it as a new incident when the feed recovers.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    seeded = normalize_alerts(
        [alert_factory(id="a", msg_type="Alert", expires="2099-01-01T00:00:00Z")]
    )
    store.process(seeded)
    hass.bus.async_fire.reset_mock()

    result = store.process([])

    assert _fired(hass) == []
    assert [a.id for a in result] == ["a"]
    assert result[0].stale is True
    assert result[0].last_confirmed  # stamped from the last cycle that saw it
    assert result[0].phase == "new"


def test_retained_alert_recovers_without_an_event(hass, alert_factory):
    """The feed comes back: the alert is confirmed again, silently."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    seeded = normalize_alerts(
        [alert_factory(id="a", msg_type="Alert", expires="2099-01-01T00:00:00Z")]
    )
    store.process(seeded)
    store.process([])
    hass.bus.async_fire.reset_mock()

    result = store.process(seeded)

    # No incident_created — the alert never left the tracked set, which is the
    # whole point: a recovered gap must not fragment the incident's history.
    assert _fired(hass) == []
    assert result[0].stale is False
    assert result[0].last_confirmed == ""


def test_absence_terminates_once_expires_has_passed(hass, alert_factory):
    """Retention is bounded by the authority's own expiry."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    # Seeded while live, so it enters the tracked set as an active alert.
    store.process(
        normalize_alerts(
            [alert_factory(id="a", msg_type="Alert", expires="2099-01-01T00:00:00Z")]
        )
    )
    hass.bus.async_fire.reset_mock()

    # A later reconciliation, with the clock past the alert's expiry. Subclassed
    # rather than mocked so ``fromisoformat`` keeps working — the store parses
    # the expiry it is comparing against.
    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2099, 6, 1, tzinfo=timezone.utc)

    with patch("custom_components.cap_alerts.store.datetime", _Later):
        result = store.process([])

    assert result == []
    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_removed"
    assert payload["phase"] == "expired"


def test_absence_without_expiry_is_retained_not_terminated(hass, alert_factory):
    """A missing ``expires`` is not a declaration that absence ends the alert.

    A field omitted from one message says there is no time-based bound,
    nothing more. NWS is retained here because it still has an exit: the
    provider fetches the VTEC ``CAN`` products the active feed omits
    (``discovers_terminations``), so a termination can still arrive. The
    alert stays visibly stale until one does — and when it does, it ends.

    Contrast ``test_absence_without_expiry_or_exit_terminates``, where nothing
    can ever end the alert and retention is therefore unsafe.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(
        normalize_alerts([alert_factory(id="a", msg_type="Alert", expires="")])
    )
    hass.bus.async_fire.reset_mock()

    result = store.process([])

    assert _fired(hass) == []
    assert [a.id for a in result] == ["a"]
    assert result[0].stale is True

    # The explicit signal is still honored: a Cancel ends the retained alert.
    store.process(
        normalize_alerts([alert_factory(id="a", msg_type="Cancel", expires="")])
    )
    assert [e for e, _ in _fired(hass)] == ["incident_removed"]


def test_absence_without_expiry_or_exit_terminates(hass, alert_factory):
    """Retention needs something that can eventually end the alert.

    The WMO shape, and not a hypothetical one: of 113 WMO authorities serving
    CAP, two — Macao and Curacao — published no ``<expires>`` on any alert,
    with nothing in the RSS envelope to fall back on. WMO declares no terminal
    vocabulary and fetches no terminations, so an expiry-less alert there has
    no exit at all: retaining it would leave an entity that never goes away.
    Absence stays authoritative for exactly that case.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "wmo")
    store.process(
        normalize_alerts(
            [alert_factory(id="a", provider="wmo", msg_type="Alert", expires="")]
        )
    )
    hass.bus.async_fire.reset_mock()

    result = store.process([])

    assert result == []
    assert [e for e, _ in _fired(hass)] == ["incident_removed"]


def test_absence_without_expiry_retained_on_a_terminal_vocabulary(hass, alert_factory):
    """ECCC has no lookup, but it does announce endings, so retention is safe."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    store.process(
        normalize_alerts(
            [alert_factory(id="a", provider="eccc", msg_type="Update", expires="")]
        )
    )
    hass.bus.async_fire.reset_mock()

    result = store.process([])

    assert _fired(hass) == []
    assert [a.id for a in result] == ["a"]
    assert result[0].stale is True


def test_absence_ends_policy_terminates_immediately(hass, alert_factory):
    """A source declaring ABSENCE_ENDS opts out of retention."""
    from custom_components.cap_alerts import store as store_mod
    from custom_components.cap_alerts.conventions import (
        ABSENCE_ENDS,
        SourceConventions,
    )
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(
        normalize_alerts(
            [alert_factory(id="a", msg_type="Alert", expires="2099-01-01T00:00:00Z")]
        )
    )
    hass.bus.async_fire.reset_mock()

    with patch.object(
        store_mod,
        "conventions_for",
        return_value=SourceConventions(absence_policy=ABSENCE_ENDS),
    ):
        result = store.process([])

    assert result == []
    assert [e for e, _ in _fired(hass)] == ["incident_removed"]


def test_absence_ends_policy_terminates_without_expiry(hass, alert_factory):
    """The convention, not the missing field, is what makes absence count."""
    from custom_components.cap_alerts import store as store_mod
    from custom_components.cap_alerts.conventions import (
        ABSENCE_ENDS,
        SourceConventions,
    )
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(
        normalize_alerts([alert_factory(id="a", msg_type="Alert", expires="")])
    )
    hass.bus.async_fire.reset_mock()

    with patch.object(
        store_mod,
        "conventions_for",
        return_value=SourceConventions(absence_policy=ABSENCE_ENDS),
    ):
        result = store.process([])

    assert result == []
    fired = _fired(hass)
    assert [e for e, _ in fired] == ["incident_removed"]
    assert fired[0][1]["phase"] == "cancel"


def test_scope_change_suspends_retention(hass, alert_factory):
    """Out of scope is not unobserved: the user moved, the alert did not."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(
        normalize_alerts(
            [alert_factory(id="a", msg_type="Alert", expires="2099-01-01T00:00:00Z")]
        )
    )
    hass.bus.async_fire.reset_mock()

    result = store.process([], scope_changed=True)

    assert result == []
    assert [e for e, _ in _fired(hass)] == ["incident_removed"]


def _eccc(alert_factory, **overrides):
    """An ECCC alert, the only source publishing a lifecycle vocabulary."""
    return alert_factory(provider="eccc", msg_type="Update", **overrides)


def test_removed_carries_removal_reason_ended(hass, alert_factory):
    """ECCC stood the alert down early: the removal says so (issue #108)."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    store.process(normalize_alerts([_eccc(alert_factory, id="a")]))
    hass.bus.async_fire.reset_mock()

    store.process(
        normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="ended")])
    )

    fired = _fired(hass)
    assert len(fired) == 1
    event_type, payload = fired[0]
    assert event_type == "incident_removed"
    assert payload["phase"] == "cancel"
    assert payload["removal_reason"] == "ended"


def test_removed_carries_removal_reason_superseded(hass, alert_factory):
    """A watch upgraded to a warning: the successor's creation carries the news."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    store.process(normalize_alerts([_eccc(alert_factory, id="a")]))
    hass.bus.async_fire.reset_mock()

    store.process(
        normalize_alerts(
            [_eccc(alert_factory, id="a", lifecycle_status="transitioned_out")]
        )
    )

    _, payload = _fired(hass)[0]
    assert payload["removal_reason"] == "superseded"


def test_removal_reason_survives_an_expired_phase(hass, alert_factory):
    """The reason is not gated on phase=cancel, and must not be.

    ECCC issues transitioned_out documents with expires at or before sent, so
    the terminal phase usually comes out "expired" — gating the reason on
    "cancel" would drop it in exactly the upgrade case it exists for.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    terminal = normalize_alerts(
        [
            _eccc(
                alert_factory,
                id="a",
                expires="2000-01-01T00:00:00Z",
                lifecycle_status="transitioned_out",
            )
        ]
    )
    store.process(terminal)

    _, payload = _fired(hass)[0]
    assert payload["phase"] == "expired"
    assert payload["removal_reason"] == "superseded"


def test_plain_cancel_has_no_removal_reason(hass, alert_factory):
    """No signal means no key — phase=cancel alone is all we know."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(normalize_alerts([alert_factory(id="a", msg_type="Cancel")]))

    _, payload = _fired(hass)[0]
    assert payload["phase"] == "cancel"
    assert "removal_reason" not in payload


def test_silent_disappearance_has_no_removal_reason(hass, alert_factory):
    """The provider dropped the record without saying why.

    The source here has a lifecycle vocabulary *and* declares absence
    authoritative, yet the silent removal still carries no reason: a reason
    requires a published token, not an inference.
    """
    from custom_components.cap_alerts import store as store_mod
    from custom_components.cap_alerts.conventions import (
        ABSENCE_ENDS,
        ECCC_LIFECYCLE_REMOVAL_REASONS,
        SourceConventions,
    )
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    store.process(
        normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="active")])
    )
    hass.bus.async_fire.reset_mock()

    with patch.object(
        store_mod,
        "conventions_for",
        return_value=SourceConventions(
            absence_policy=ABSENCE_ENDS,
            lifecycle_removal_reasons=ECCC_LIFECYCLE_REMOVAL_REASONS,
        ),
    ):
        store.process([])

    _, payload = _fired(hass)[0]
    assert payload["phase"] == "cancel"
    assert "removal_reason" not in payload


def test_removal_reason_is_scoped_to_its_source(hass, alert_factory):
    """ECCC's vocabulary cannot label another source's removal (issue #82)."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "nws")
    store.process(
        normalize_alerts(
            [alert_factory(id="a", msg_type="Cancel", lifecycle_status="ended")]
        )
    )

    _, payload = _fired(hass)[0]
    assert "removal_reason" not in payload


def test_created_and_updated_have_no_removal_reason(hass, alert_factory):
    """The key rides on incident_removed only."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")
    store.process(
        normalize_alerts([_eccc(alert_factory, id="a", lifecycle_status="active")])
    )
    store.process(
        normalize_alerts(
            [
                _eccc(
                    alert_factory, id="a", lifecycle_status="active", headline="revised"
                )
            ]
        )
    )

    fired = _fired(hass)
    assert [event_type for event_type, _ in fired] == [
        "incident_created",
        "incident_updated",
    ]
    assert all("removal_reason" not in payload for _, payload in fired)


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
