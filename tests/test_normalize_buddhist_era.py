"""Buddhist-Era year correction in CAP dateTime fields.

Thai feeds (TMD, surfaced via WMO SWIC) emit Buddhist-Era years (Gregorian +
543) in CAP timestamps, e.g. "2568-08-05T22:50:00+07:00". The integration
rewrites only the year — the Thai solar calendar is Gregorian apart from the
era number — so month, day, time, and UTC offset are preserved.
"""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.normalize import _gregorian, normalize_alerts


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Buddhist-Era → Gregorian, offset and time preserved verbatim.
        ("2568-08-05T22:50:00+07:00", "2025-08-05T22:50:00+07:00"),
        ("2568-08-05T22:50:00Z", "2025-08-05T22:50:00Z"),
        ("2567-12-31", "2024-12-31"),  # date-only, no time component
        # Already Gregorian — left untouched.
        ("2025-08-05T22:50:00+07:00", "2025-08-05T22:50:00+07:00"),
        ("1999-01-01T00:00:00Z", "1999-01-01T00:00:00Z"),
        # No leading 4-digit year, or empty — passthrough.
        ("", ""),
        ("not-a-date", "not-a-date"),
        ("99-08-05", "99-08-05"),
    ],
)
def test_gregorian_rewrites_only_buddhist_era_years(value, expected):
    assert _gregorian(value) == expected


def test_normalize_corrects_all_timestamp_fields(alert_factory):
    # Every CAP timestamp field carrying a BE year is corrected on output.
    alert = alert_factory(
        provider="wmo",
        sent="2568-08-05T22:50:00+07:00",
        effective="2568-08-05T22:50:00+07:00",
        onset="2568-08-05T22:50:00+07:00",
        expires="2568-08-06T11:00:00+07:00",
    )
    (out,) = normalize_alerts([alert])
    assert out.sent == "2025-08-05T22:50:00+07:00"
    assert out.effective == "2025-08-05T22:50:00+07:00"
    assert out.onset == "2025-08-05T22:50:00+07:00"
    assert out.expires == "2025-08-06T11:00:00+07:00"


def test_be_alert_past_its_real_expiry_is_marked_expired(alert_factory):
    # 2568-01-01 → 2025-01-01, which is in the past: without the year fix the
    # raw "2568" reads ~543 years in the future and never expires.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="wmo", msg_type="Alert", expires="2568-01-01T00:00:00Z"
            )
        ]
    )
    assert out.phase == "expired"


def test_be_alert_before_its_real_expiry_is_not_expired(alert_factory):
    # 2599-12-31 → 2056-12-31, still in the future: phase follows msg_type.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="wmo", msg_type="Alert", expires="2599-12-31T00:00:00Z"
            )
        ]
    )
    assert out.phase == "new"
