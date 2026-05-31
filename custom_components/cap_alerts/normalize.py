"""Shared normalization applied after provider fetch."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

from .const import BUDDHIST_ERA_OFFSET, MIN_BUDDHIST_ERA_YEAR
from .icons import icon_for
from .model import CAPAlert

MAX_STATE_LENGTH = 255
SOFT_CAP_BYTES = 4096

# Some feeds — notably TMD, surfaced via WMO SWIC — emit Buddhist-Era years
# (Gregorian + 543) in CAP dateTime fields, e.g. "2568-08-05T22:50:00+07:00".
# Left as-is, _compute_phase never expires the alert and the card renders
# "STARTS IN 198034d". The detection threshold (MIN_BUDDHIST_ERA_YEAR) lives in
# const.py, shared with the WMO RSS-envelope correction. Only the year is
# rewritten; the time and UTC offset are preserved verbatim.
_ISO_YEAR_RE = re.compile(r"^(\d{4})(\D.*)$")

# CAP canonical severity set (RFC §2.1). Anything outside clamps to "unknown".
_CANONICAL_SEVERITIES = frozenset({"extreme", "severe", "moderate", "minor", "unknown"})

# VTEC significance → severity tier
_VTEC_SIG_SEVERITY = {
    "W": "severe",  # Warning
    "A": "moderate",  # Watch
    "Y": "minor",  # Advisory
    "S": "unknown",  # Statement
}

# Phenomena codes that escalate a Warning to "extreme"
_VTEC_EXTREME_PHENOMENA = {"TO", "EW"}  # Tornado, Extreme Wind

# MeteoAlarm awareness color → CAP canonical tier. Green has no analogue on
# the canonical axis (no "none" tier), so it lands on the neutral "unknown"
# rather than the misleading "minor".
_METEOALARM_AWARENESS_TO_SEVERITY = {
    "green": "unknown",
    "yellow": "moderate",
    "orange": "severe",
    "red": "extreme",
}


def normalize_alerts(alerts: list[CAPAlert], entry_id: str = "") -> list[CAPAlert]:
    """Apply shared normalization to a list of provider-parsed alerts.

    ``entry_id`` namespaces each alert's ``geometry_ref`` so config entries
    that share a provider don't evict one another's geometry from the shared
    store. Defaults to "" for callers that don't externalize geometry.
    """
    now = datetime.now(timezone.utc)
    return [_normalize(a, now, entry_id) for a in alerts]


def _geometry_ref(alert: CAPAlert, entry_id: str) -> str:
    """Build the externalized-geometry handle, or "" when no geometry.

    Namespaced by config entry: the geometry store is a process-wide
    singleton shared across entries, so two entries on the same provider
    would otherwise mint identical ``{provider}:{id}`` refs and each entry's
    purge would evict the other's polygons (RFC §2.4).
    """
    if not alert.geometry:
        return ""
    scope = f"{entry_id}:" if entry_id else ""
    return f"{scope}{alert.provider}:{alert.id}"


def _normalize(alert: CAPAlert, now: datetime, entry_id: str = "") -> CAPAlert:
    """Normalize a single alert. Returns a new frozen instance."""
    sent = _gregorian(alert.sent)
    effective = _gregorian(alert.effective)
    onset = _gregorian(alert.onset)
    expires = _gregorian(alert.expires)
    return replace(
        alert,
        sent=sent,
        effective=effective,
        onset=onset,
        expires=expires,
        event=_truncate_state(alert.event),
        severity_normalized=_normalize_severity(alert),
        phase=_compute_phase(expires, alert.msg_type, now),
        icon=icon_for(alert),
        bbox=_bbox_from_geometry(alert.geometry),
        geometry_ref=_geometry_ref(alert, entry_id),
        description=_soft_cap(alert.description),
        instruction=_soft_cap(alert.instruction)
        if alert.instruction
        else alert.instruction,
    )


def _normalize_severity(alert: CAPAlert) -> str:
    """Map provider-native severity to lowercase CAP canonical value.

    CAP canonical: extreme, severe, moderate, minor, unknown. Any value
    outside that set — including provider-specific strings — clamps to
    "unknown" so the entity state stays on the five-value axis that the
    frontend styles against (RFC §2.1).
    """
    if alert.provider == "nws":
        raw = _nws_severity(alert)
    elif alert.provider == "meteoalarm" and (
        awareness := _meteoalarm_awareness_severity(alert)
    ):
        raw = awareness
    elif alert.severity:
        raw = alert.severity.lower()
    else:
        raw = "unknown"
    return raw if raw in _CANONICAL_SEVERITIES else "unknown"


def _meteoalarm_awareness_severity(alert: CAPAlert) -> str | None:
    """Map MeteoAlarm ``awareness_level`` to a canonical severity, or None.

    The parameter format published by EUMETNET members is ``"N; color; Label"``
    (e.g. ``"3; orange; Severe"``). The color token is the contract; the
    numeric prefix and trailing label are ignored. Returns ``None`` for
    missing, malformed, or unrecognized values so the caller falls back to
    CAP ``severity``.
    """
    if alert.parameters is None:
        return None
    raw = alert.parameters.get("awareness_level")
    if not raw:
        return None
    parts = raw.split(";")
    if len(parts) < 2:
        return None
    color = parts[1].strip().lower()
    return _METEOALARM_AWARENESS_TO_SEVERITY.get(color)


def _nws_severity(alert: CAPAlert) -> str:
    """Derive severity from VTEC codes (authoritative for NWS)."""
    sig = alert.vtec_significance
    if not sig:
        return alert.severity.lower() if alert.severity else "unknown"
    # Tornado/Extreme Wind warnings are "extreme", not just "severe"
    if sig == "W" and alert.vtec_phenomena in _VTEC_EXTREME_PHENOMENA:
        return "extreme"
    return _VTEC_SIG_SEVERITY.get(sig, "unknown")


def _gregorian(value: str) -> str:
    """Rewrite a Buddhist-Era year in a CAP dateTime to Gregorian.

    Returns ``value`` unchanged when it lacks a leading 4-digit year or the
    year is already Gregorian (< 2400). Only the year is touched; month, day,
    time, and UTC offset are preserved verbatim — the Thai solar calendar is
    Gregorian apart from the era number (BE = CE + 543).
    """
    if not value:
        return value
    m = _ISO_YEAR_RE.match(value)
    if m is None:
        return value
    year = int(m.group(1))
    if year < MIN_BUDDHIST_ERA_YEAR:
        return value
    return f"{year - BUDDHIST_ERA_OFFSET:04d}{m.group(2)}"


def _compute_phase(expires: str, msg_type: str, now: datetime) -> str:
    """Lifecycle phase: ``expired`` if past ``expires``, else from msg_type."""
    expires_at = _parse_iso(expires)
    if expires_at is not None and now > expires_at:
        return "expired"
    return _normalize_phase(msg_type)


def _normalize_phase(msg_type: str) -> str:
    """Map msg_type to lowercase lifecycle phase.

    Known CAP msg_types map: ``Alert → new``, ``Update → update``,
    ``Cancel → cancel``. Any other value (provider-specific vocabulary such
    as ECCC's ``Actual``, or a missing field) defaults to ``"new"`` so the
    RFC §2.1 guarantee that ``phase`` is always one of
    ``{new, update, cancel, expired}`` is never broken.
    """
    return {
        "Alert": "new",
        "Update": "update",
        "Cancel": "cancel",
    }.get(msg_type, "new")


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
