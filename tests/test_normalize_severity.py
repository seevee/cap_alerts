"""Severity normalization: canonical CAP set + clamping of foreign values."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from custom_components.cap_alerts.normalize import normalize_alerts

_CANONICAL = {"extreme", "severe", "moderate", "minor", "unknown"}
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize("value", ["Extreme", "Severe", "Moderate", "Minor", "Unknown"])
def test_cap_native_provider_lowercases_canonical_value(alert_factory, value):
    (out,) = normalize_alerts(
        [alert_factory(provider="eccc", severity=value, msg_type="Alert")]
    )
    assert out.severity_normalized == value.lower()


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        ("green", "unknown"),
        ("yellow", "moderate"),
        ("orange", "severe"),
        ("red", "extreme"),
    ],
)
def test_meteoalarm_awareness_level_drives_severity(alert_factory, color, expected):
    # awareness_level is the public-facing color tier; it must take precedence
    # over the upstream CAP severity (which historically diverged from the
    # color shown to end users).
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="meteoalarm",
                severity="Minor",
                msg_type="Alert",
                parameters={"awareness_level": f"3; {color}; Label"},
            )
        ]
    )
    assert out.severity_normalized == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Extreme", "extreme"),
        ("Severe", "severe"),
        ("Moderate", "moderate"),
        ("Minor", "minor"),
        ("Unknown", "unknown"),
        ("orange", "unknown"),  # foreign string clamps to canonical "unknown"
        ("", "unknown"),
    ],
)
def test_meteoalarm_falls_back_to_cap_severity_when_awareness_missing(
    alert_factory, value, expected
):
    # No awareness_level parameter → behaviour matches the pre-awareness
    # implementation: lowercased CAP severity, clamped to canonical.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="meteoalarm",
                severity=value,
                msg_type="Alert",
                parameters=None,
            )
        ]
    )
    assert out.severity_normalized == expected


@pytest.mark.parametrize(
    "awareness_level",
    ["", "3", "3; ", "; orange; Severe", "3;PURPLE;weird"],
)
def test_meteoalarm_unparseable_awareness_falls_through(alert_factory, awareness_level):
    # Unparseable awareness must not poison the output; CAP severity (or its
    # canonical clamp) takes over so we always land on the canonical axis.
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="meteoalarm",
                severity="Severe",
                msg_type="Alert",
                parameters={"awareness_level": awareness_level},
            )
        ]
    )
    assert out.severity_normalized in _CANONICAL


def test_meteoalarm_case_insensitive_color(alert_factory):
    (out,) = normalize_alerts(
        [
            alert_factory(
                provider="meteoalarm",
                severity="Minor",
                msg_type="Alert",
                parameters={"awareness_level": "4; RED; severe"},
            )
        ]
    )
    assert out.severity_normalized == "extreme"


def test_meteoalarm_de_fixture_orange_yellow_map_correctly():
    # End-to-end: parse the committed DE fixture, normalize, and assert the
    # awareness color (not CAP severity) drives the final state.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from test_meteoalarm_parser import _load_meteoalarm
    finally:
        sys.path.pop(0)
    meteoalarm = _load_meteoalarm()
    feed = json.loads((_FIXTURE_DIR / "meteoalarm_de.json").read_text(encoding="utf-8"))
    parsed = []
    for warning in feed["warnings"]:
        alert = meteoalarm._warning_to_alert(warning, "de")
        if alert is not None:
            parsed.append(alert)
    by_event = {a.event: a for a in normalize_alerts(parsed)}
    assert by_event["STURMBÖEN"].severity_normalized == "severe"  # orange
    assert by_event["FROST"].severity_normalized == "moderate"  # yellow


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
