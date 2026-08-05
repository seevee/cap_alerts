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


def test_lifecycle_status_ended_is_terminal(alert_factory):
    # ECCC never signals termination through msgType — it stays "Update" and
    # leaves an hour of expires on the clock, marking the area group "ended" in
    # a CAP parameter instead (issue #45). Without honouring that, the alert
    # stays live with a headline that literally says it ended.
    #
    # "cancel", not "expired": expires is still in the future, so the alert
    # ended early (issue #95). A consumer that only sees "expired" cannot tell
    # this from an alert that ran its full course.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="eccc",
                msg_type="Update",
                expires="2099-01-01T00:00:00Z",
                lifecycle_status="ended",
            )
        ]
    )
    assert out.phase == "cancel"


def test_lifecycle_status_transitioned_out_is_terminal(alert_factory):
    # The area moved to a different alert (yellow → orange), which arrives as
    # its own document; this one is over for the area it covers.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="eccc",
                msg_type="Update",
                expires="2099-01-01T00:00:00Z",
                lifecycle_status="transitioned_out",
            )
        ]
    )
    assert out.phase == "cancel"


def test_expired_wins_over_a_terminal_lifecycle_status(alert_factory):
    # Ordering is load-bearing (issue #95). An alert can be past its expiry and
    # carry a terminal status in the same document; that one ran its course, so
    # the clock decides and "expired" must not be downgraded to "cancel".
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="eccc",
                msg_type="Update",
                expires="2020-01-01T00:00:00Z",
                lifecycle_status="ended",
            )
        ]
    )
    assert out.phase == "expired"


def test_unknown_lifecycle_status_is_not_terminal(alert_factory):
    # Fail open: only the two known terminal tokens retire an alert. An
    # unfamiliar value must degrade to msg_type handling, never to a false
    # all-clear.
    for status in ("active", "wat"):
        (out,) = normalize_alerts(
            [
                alert_factory(
                    provider="eccc",
                    msg_type="Update",
                    expires="2099-01-01T00:00:00Z",
                    lifecycle_status=status,
                )
            ]
        )
        assert out.phase == "update", status


def test_terminal_tokens_are_scoped_to_their_source(alert_factory):
    # Terminal vocabulary is read from the source's convention table entry
    # (issue #82), so ECCC's tokens cannot retire another source's alerts.
    # No other provider sets lifecycle_status today, which is what makes this
    # scoping inert in production rather than a behaviour change.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="nws",
                msg_type="Update",
                expires="2099-01-01T00:00:00Z",
                lifecycle_status="ended",
            )
        ]
    )
    assert out.phase == "update"


def test_empty_lifecycle_status_leaves_phase_unchanged(alert_factory):
    # Regression guard for every provider that publishes no such signal.
    (out,) = normalize_alerts(
        [alert_factory(msg_type="Update", expires="2099-01-01T00:00:00Z")]
    )
    assert out.lifecycle_status == ""
    assert out.phase == "update"


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
