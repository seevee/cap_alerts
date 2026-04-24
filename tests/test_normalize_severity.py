"""Severity normalization: canonical CAP set + clamping of foreign values."""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.normalize import normalize_alerts

_CANONICAL = {"extreme", "severe", "moderate", "minor", "unknown"}


@pytest.mark.parametrize("value", ["Extreme", "Severe", "Moderate", "Minor", "Unknown"])
def test_cap_native_provider_lowercases_canonical_value(alert_factory, value):
    (out,) = normalize_alerts(
        [alert_factory(provider="eccc", severity=value, msg_type="Alert")]
    )
    assert out.severity_normalized == value.lower()


def test_normalize_severity_unknown_clamps_non_cap(alert_factory):
    # A provider that returns a non-canonical string like "foo" must not
    # leak that value out as the entity state (RFC §2.1).
    (out,) = normalize_alerts(
        [alert_factory(provider="eccc", severity="foo", msg_type="Alert")]
    )
    assert out.severity_normalized == "unknown"


def test_missing_severity_is_unknown(alert_factory):
    (out,) = normalize_alerts(
        [alert_factory(provider="eccc", severity="", msg_type="Alert")]
    )
    assert out.severity_normalized == "unknown"


def test_nws_without_vtec_clamps_foreign_severity(alert_factory):
    # NWS branch falls through to the CAP severity string when VTEC is absent;
    # that string must still be clamped.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="nws",
                vtec_significance="",
                severity="bogus",
                msg_type="Alert",
            )
        ]
    )
    assert out.severity_normalized == "unknown"


def test_nws_tornado_warning_is_extreme(alert_factory):
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="nws",
                vtec_significance="W",
                vtec_phenomena="TO",
                msg_type="Alert",
            )
        ]
    )
    assert out.severity_normalized == "extreme"


def test_every_normalized_severity_is_canonical(alert_factory):
    # Sweep several provider/severity combos and assert the output stays on
    # the canonical axis even when the input is garbage.
    inputs = [
        ("nws", "", ""),
        ("nws", "W", ""),
        ("nws", "", "gibberish"),
        ("eccc", "", "severe"),
        ("eccc", "", "LOUD"),
        ("eccc", "", ""),
    ]
    alerts = [
        alert_factory(provider=p, vtec_significance=sig, severity=sev, msg_type="Alert")
        for (p, sig, sev) in inputs
    ]
    for out in normalize_alerts(alerts):
        assert out.severity_normalized in _CANONICAL
