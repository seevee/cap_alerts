"""Shared normalization applied after provider fetch."""

from __future__ import annotations

from dataclasses import replace

from .model import CAPAlert

MAX_STATE_LENGTH = 255


def normalize_alerts(alerts: list[CAPAlert]) -> list[CAPAlert]:
    """Apply shared normalization to a list of provider-parsed alerts."""
    return [_normalize(a) for a in alerts]


def _normalize(alert: CAPAlert) -> CAPAlert:
    """Normalize a single alert. Returns a new frozen instance."""
    return replace(
        alert,
        event=_truncate_state(alert.event),
        severity_normalized=_normalize_severity(alert.severity, alert.provider),
        phase=_normalize_phase(alert.msg_type),
    )


def _normalize_severity(severity: str, provider: str) -> str:
    """Map provider-native severity to lowercase CAP canonical value.

    CAP canonical: extreme, severe, moderate, minor, unknown.
    """
    if not severity:
        return "unknown"
    # CAP-native providers (NWS, ECCC, MeteoAlarm) — already CAP values
    return severity.lower()


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
