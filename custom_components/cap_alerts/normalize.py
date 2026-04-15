"""Shared normalization applied after provider fetch."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from .icons import icon_for
from .model import CAPAlert

MAX_STATE_LENGTH = 255
SOFT_CAP_BYTES = 4096

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
    now = datetime.now(timezone.utc)
    return [_normalize(a, now) for a in alerts]


def filter_active_alerts(alerts: list[CAPAlert]) -> list[CAPAlert]:
    """Remove cancelled and expired alerts. Applied after normalization."""
    return [a for a in alerts if a.phase not in ("cancel", "expired")]


def _normalize(alert: CAPAlert, now: datetime) -> CAPAlert:
    """Normalize a single alert. Returns a new frozen instance."""
    return replace(
        alert,
        event=_truncate_state(alert.event),
        severity_normalized=_normalize_severity(alert),
        phase=_compute_phase(alert, now),
        icon=icon_for(alert),
        bbox=_bbox_from_geometry(alert.geometry),
        geometry_ref=f"{alert.provider}:{alert.id}" if alert.geometry else "",
        description=_soft_cap(alert.description),
        instruction=_soft_cap(alert.instruction) if alert.instruction else alert.instruction,
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


def _compute_phase(alert: CAPAlert, now: datetime) -> str:
    """Lifecycle phase: ``expired`` if past ``expires``, else from msg_type."""
    expires_at = _parse_iso(alert.expires)
    if expires_at is not None and now > expires_at:
        return "expired"
    return _normalize_phase(alert.msg_type)


def _normalize_phase(msg_type: str) -> str:
    """Map msg_type to lowercase lifecycle phase."""
    return {
        "Alert": "new",
        "Update": "update",
        "Cancel": "cancel",
    }.get(msg_type, "")


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp; return None on any failure."""
    if not value:
        return None
    try:
        # datetime.fromisoformat handles offsets in 3.11+; normalize 'Z'.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bbox_from_geometry(
    geometry: dict | None,
) -> tuple[float, float, float, float] | None:
    """Return ``(min_lon, min_lat, max_lon, max_lat)`` from a GeoJSON geometry.

    Supports Point, LineString, Polygon, MultiPolygon. Returns ``None`` when
    geometry is missing, malformed, or contains no usable coordinates.
    """
    if not geometry:
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if coords is None:
        return None

    points: list[tuple[float, float]] = []
    try:
        if gtype == "Point":
            points.append((float(coords[0]), float(coords[1])))
        elif gtype == "LineString":
            for c in coords:
                points.append((float(c[0]), float(c[1])))
        elif gtype == "Polygon":
            for ring in coords:
                for c in ring:
                    points.append((float(c[0]), float(c[1])))
        elif gtype == "MultiPolygon":
            for poly in coords:
                for ring in poly:
                    for c in ring:
                        points.append((float(c[0]), float(c[1])))
        else:
            return None
    except (TypeError, ValueError, IndexError):
        return None

    if not points:
        return None

    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (min(lons), min(lats), max(lons), max(lats))


def _soft_cap(text: str, limit_bytes: int = SOFT_CAP_BYTES) -> str:
    """Trim ``text`` to ``limit_bytes`` UTF-8 bytes, appending ``…``.

    Truncates at a UTF-8 character boundary to avoid mojibake. Under-limit
    input is returned unchanged.
    """
    if not text:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    # Reserve 3 bytes for the trailing ellipsis (U+2026 is 3 bytes in UTF-8).
    budget = limit_bytes - 3
    truncated = encoded[:budget]
    # Back off to a character boundary by decoding with 'ignore'.
    return truncated.decode("utf-8", errors="ignore") + "\u2026"


def _truncate_state(value: str) -> str:
    """Truncate to HA's 255-character state limit."""
    if len(value) <= MAX_STATE_LENGTH:
        return value
    return value[: MAX_STATE_LENGTH - 1] + "\u2026"
