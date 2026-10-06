"""Buddhist-Era year correction in WMO CAP dateTime fields.

Thai feeds (TMD, surfaced via WMO SWIC) emit Buddhist-Era years (Gregorian +
543) in CAP timestamps, e.g. "2568-08-05T22:50:00+07:00". The WMO provider
rewrites only the year as it builds the alert — the Thai solar calendar is
Gregorian apart from the era number — so month, day, time, and UTC offset are
preserved, and normalization downstream sees Gregorian dates.
"""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.normalize import normalize_alerts
from custom_components.cap_alerts.providers.cap import parse_cap_alert
from custom_components.cap_alerts.providers.wmo import _build_alert, _gregorian


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


def _tmd_alert(
    *,
    sent: str = "2568-08-05T22:50:00+07:00",
    effective: str = "2568-08-05T22:50:00+07:00",
    onset: str = "2568-08-05T22:50:00+07:00",
    expires: str = "2568-08-06T11:00:00+07:00",
):
    """A minimal TMD-shaped CAP body, built the way the provider builds it."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>tmd-1</identifier><sender>tmd@tmd.go.th</sender>"
        f"<sent>{sent}</sent><status>Actual</status><msgType>Alert</msgType>"
        "<scope>Public</scope><info><language>th</language><category>Met</category>"
        "<event>Heavy rain</event><urgency>Expected</urgency>"
        "<severity>Moderate</severity><certainty>Likely</certainty>"
        f"<effective>{effective}</effective><onset>{onset}</onset>"
        f"<expires>{expires}</expires><headline>Heavy rain</headline>"
        "<area><areaDesc>Bangkok</areaDesc></area></info></alert>"
    )
    doc = parse_cap_alert(xml)
    assert doc is not None
    return _build_alert(doc, doc.infos[0], "https://example.invalid/tmd-1", "tmd-1")


def test_build_corrects_all_timestamp_fields():
    # Every CAP timestamp field carrying a BE year is corrected on construction.
    alert = _tmd_alert()
    assert alert.sent == "2025-08-05T22:50:00+07:00"
    assert alert.effective == "2025-08-05T22:50:00+07:00"
    assert alert.onset == "2025-08-05T22:50:00+07:00"
    assert alert.expires == "2025-08-06T11:00:00+07:00"


def test_be_alert_past_its_real_expiry_is_marked_expired():
    # 2568-01-01 → 2025-01-01, which is in the past: without the year fix the
    # raw "2568" reads ~543 years in the future and never expires.
    (out,) = normalize_alerts([_tmd_alert(expires="2568-01-01T00:00:00Z")])
    assert out.phase == "expired"


def test_be_alert_before_its_real_expiry_is_not_expired():
    # 2599-12-31 → 2056-12-31, still in the future: phase follows msg_type.
    (out,) = normalize_alerts([_tmd_alert(expires="2599-12-31T00:00:00Z")])
    assert out.phase == "new"
