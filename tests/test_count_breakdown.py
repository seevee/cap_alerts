"""Active vs. upcoming breakdown on the count sensor (issue #99)."""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.cap_alerts.normalize import count_by_onset

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def test_future_onset_counts_as_upcoming(alert_factory):
    alerts = [alert_factory(onset="2026-08-06T00:00:00+00:00")]
    assert count_by_onset(alerts, NOW) == (0, 1)


def test_past_onset_counts_as_active(alert_factory):
    alerts = [alert_factory(onset="2026-08-05T06:00:00+00:00")]
    assert count_by_onset(alerts, NOW) == (1, 0)


def test_missing_onset_counts_as_active(alert_factory):
    # A provider that omits onset is describing something already in force.
    alerts = [alert_factory(onset="")]
    assert count_by_onset(alerts, NOW) == (1, 0)


def test_unparseable_onset_counts_as_active(alert_factory):
    alerts = [alert_factory(onset="not a timestamp")]
    assert count_by_onset(alerts, NOW) == (1, 0)


def test_onset_exactly_now_counts_as_active(alert_factory):
    alerts = [alert_factory(onset="2026-08-05T12:00:00+00:00")]
    assert count_by_onset(alerts, NOW) == (1, 0)


def test_non_utc_offset_compared_as_an_instant(alert_factory):
    # 14:00+03:00 is 11:00Z — already in force, despite the later wall clock.
    alerts = [alert_factory(onset="2026-08-05T14:00:00+03:00")]
    assert count_by_onset(alerts, NOW) == (1, 0)


def test_mixed_set_splits_and_sums_to_the_total(alert_factory):
    alerts = [
        alert_factory(id="a", onset="2026-08-05T06:00:00+00:00"),
        alert_factory(id="b", onset=""),
        alert_factory(id="c", onset="2026-08-06T00:00:00+00:00"),
        alert_factory(id="d", onset="2026-08-07T00:00:00+00:00"),
    ]
    active, upcoming = count_by_onset(alerts, NOW)
    assert (active, upcoming) == (2, 2)
    assert active + upcoming == len(alerts)


def test_empty_list_is_zero_zero():
    assert count_by_onset([], NOW) == (0, 0)
