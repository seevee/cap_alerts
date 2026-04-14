"""Shared normalization applied after provider fetch."""

from __future__ import annotations

from dataclasses import replace

from .model import CAPAlert

MAX_STATE_LENGTH = 255

# VTEC significance → severity tier
_VTEC_SIG_SEVERITY = {
    "W": "severe",    # Warning
    "A": "moderate",  # Watch
    "Y": "minor",     # Advisory
    "S": "unknown",   # Statement
}

# Phenomena codes that escalate a Warning to "extreme"
_VTEC_EXTREME_PHENOMENA = {"TO", "EW"}  # Tornado, Extreme Wind


def normalize_alerts(alerts: list[CAPAlert]) -> list[CAPAlert]:
    """Apply shared normalization to a list of provider-parsed alerts."""
    return [_normalize(a) for a in alerts]


def filter_active_alerts(alerts: list[CAPAlert]) -> list[CAPAlert]:
    """Remove cancelled alerts. Applied after normalization."""
    return [a for a in alerts if a.phase != "Cancel"]


def _normalize(alert: CAPAlert) -> CAPAlert:
    """Normalize a single alert. Returns a new frozen instance."""
    return replace(
        alert,
        event=_truncate_state(alert.event),
        severity_normalized=_normalize_severity(alert),
        phase=_normalize_phase(alert.msg_type),
    )


def _normalize_severity(alert: CAPAlert) -> str:
    """Map provider-native severity to lowercase CAP canonical value.

    CAP canonical: extreme, severe, moderate, minor, unknown.
    For NWS, VTEC significance/phenomena codes are authoritative.
    """
    if alert.provider == "nws":
        return _nws_severity(alert)
    # Default: trust CAP severity field
    return alert.severity.lower() if alert.severity else "unknown"


def _nws_severity(alert: CAPAlert) -> str:
    """Derive severity from VTEC codes (authoritative for NWS)."""
    sig = alert.vtec_significance
    if not sig:
        return alert.severity.lower() if alert.severity else "unknown"
    # Tornado/Extreme Wind warnings are "extreme", not just "severe"
    if sig == "W" and alert.vtec_phenomena in _VTEC_EXTREME_PHENOMENA:
        return "extreme"
    return _VTEC_SIG_SEVERITY.get(sig, "unknown")


def _normalize_phase(msg_type: str) -> str:
    """Map msg_type to lifecycle phase."""
    return {
        "Alert": "New",
        "Update": "Update",
        "Cancel": "Cancel",
    }.get(msg_type, "")


def _truncate_state(value: str) -> str:
    """Truncate to HA's 255-character state limit."""
    if len(value) <= MAX_STATE_LENGTH:
        return value
    return value[: MAX_STATE_LENGTH - 1] + "\u2026"
