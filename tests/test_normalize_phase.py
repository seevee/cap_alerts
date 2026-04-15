"""Phase normalization: lowercase vocabulary + expired detection."""

from __future__ import annotations

from custom_components.cap_alerts.normalize import (
    filter_active_alerts,
    normalize_alerts,
)


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


def test_filter_drops_cancel_and_expired(alert_factory):
    alerts = normalize_alerts(
        [
            alert_factory(id="a", msg_type="Alert"),
            alert_factory(id="b", msg_type="Cancel"),
            alert_factory(id="c", msg_type="Alert", expires="2000-01-01T00:00:00Z"),
        ]
    )
    kept = filter_active_alerts(alerts)
    assert [a.id for a in kept] == ["a"]
