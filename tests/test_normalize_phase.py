"""Phase normalization: lowercase vocabulary + expired detection."""

from __future__ import annotations

from custom_components.cap_alerts.normalize import normalize_alerts


def test_msg_type_alert_becomes_new_lowercase(alert_factory):
    (out,) = normalize_alerts([alert_factory(msg_type="Alert")])
    assert out.phase == "new"


def test_msg_type_update_becomes_update_lowercase(alert_factory):
    (out,) = normalize_alerts([alert_factory(msg_type="Update")])
    assert out.phase == "update"


def test_msg_type_cancel_becomes_cancel_lowercase(alert_factory):
    (out,) = normalize_alerts([alert_factory(msg_type="Cancel")])
    assert out.phase == "cancel"


def test_past_expires_becomes_expired(alert_factory):
    (out,) = normalize_alerts(
        [alert_factory(msg_type="Alert", expires="2000-01-01T00:00:00Z")]
    )
    assert out.phase == "expired"


def test_unknown_msg_type_defaults_to_new(alert_factory):
    # ECCC occasionally uses msg_types outside the CAP {Alert, Update, Cancel}
    # set (e.g. "Actual"). The RFC requires phase to always be one of the
    # four canonical values, so unknown codes default to "new".
    (out,) = normalize_alerts([alert_factory(msg_type="Actual")])
    assert out.phase == "new"


def test_missing_msg_type_defaults_to_new(alert_factory):
    (out,) = normalize_alerts([alert_factory(msg_type="")])
    assert out.phase == "new"


def test_phase_is_never_empty(alert_factory):
    # Guard rail: across a sweep of odd msg_type values, phase must always
    # fall on {new, update, cancel, expired} — never "".
    canonical = {"new", "update", "cancel", "expired"}
    samples = [
        alert_factory(id="a", msg_type="Alert"),
        alert_factory(id="b", msg_type="Update"),
        alert_factory(id="c", msg_type="Cancel"),
        alert_factory(id="d", msg_type="Actual"),
        alert_factory(id="e", msg_type=""),
        alert_factory(id="f", msg_type="WeirdVocab"),
        alert_factory(id="g", msg_type="Alert", expires="2000-01-01T00:00:00Z"),
    ]
    for out in normalize_alerts(samples):
        assert out.phase in canonical
