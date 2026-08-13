"""Config-entry diagnostics — the support artifact (issue #134).

The three diagnostic *entities* are dashboard surfaces. This is the other
thing: one download that answers "what is this entry actually doing" without
asking a reporter to enable debug logging and paste back a wall of text.

Two rules shape what lands in the payload.

**Nothing that has to be re-derived.** Everything here is read off the
coordinator as it stands. In particular the *resolved* config — tracker to
coordinates, country entity to ISO-2, language ``auto`` to a concrete tag — is
the pair the last update recorded, never a fresh ``_resolve_config()`` call:
that resolution owns the scope key retention decisions are made against, so
running it here would consume a scope change the next real cycle needs to see.

**Nothing that makes the dump unsafe to paste.** A diagnostics download usually
ends up in a public issue. GPS coordinates, the tracker entity and the
MeteoAlarm country-source entity are redacted wherever they appear, including
inside a provider endpoint built from them. Credential keys go through the same
list even though no shipped provider authenticates today — the first one that
does should not have to remember this file exists.

Raw alert bodies are left out: they are large, and geometry is already
externalized (RFC §2.4) for that reason. What each alert contributes is its
lifecycle — id, phase, timestamps, the sender whose dialect applies — which is
what a report about a stuck or missing entity turns on.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_API_KEY,
    CONF_CLIENT_SECRET,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import REDACTED

from .const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ENTITY,
    CONF_EXCLUDE_MARINE,
    CONF_FEED_SOURCE,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_REGIONS,
    CONF_SOURCE_ID,
    CONF_TIMEOUT,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
    DEFAULT_FEED_SOURCE,
    DEFAULT_TIMEOUT,
    GDACS_RSS_24H_URL,
    GDACS_RSS_CURRENT_URL,
    METEOALARM_COUNTRY_SLUGS,
    NAAD_STREAM_HOST,
    NAAD_STREAM_PORT,
    PLATFORM_VERSION,
)
from .conventions import CONVENTIONS, PipelineStage, SourceConventions, conventions_for
from .model import CAPAlert
from .normalize import count_by_onset
from .providers.eccc import resolve_feed_urls
from .providers.meteoalarm import METEOALARM_FEED_URL
from .providers.nws import NWS_ALL_BASE, NWS_API_BASE
from .providers.wmo import WMO_RSS_URL

if TYPE_CHECKING:
    from . import CAPAlertsConfigEntry

# Redacted wherever they appear — in entry data, in resolved data, and in a
# built endpoint. The location keys are the ones a reporter cannot take back
# once pasted; the credential keys are the wiring the issue asked for ahead of
# the first provider that needs it.
TO_REDACT = {
    CONF_GPS_LOC,
    CONF_TRACKER_ENTITY,
    CONF_COUNTRY_ENTITY,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_ACCESS_TOKEN,
    CONF_API_KEY,
    CONF_CLIENT_SECRET,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_USERNAME,
}

# Ceiling on the per-alert lifecycle table. A GDACS entry at the green floor
# can carry a few hundred alerts, and the rows past the first hundred add
# weight without adding an answer.
MAX_ALERT_ROWS = 100

# Location modes, most specific first — the same precedence the entry title
# uses, so MeteoAlarm's fully-mobile mode reports as a country source rather
# than as the tracker it happens to also carry.
_SCOPE_KEYS: tuple[tuple[str, str], ...] = (
    (CONF_COUNTRY_ENTITY, "country_source"),
    (CONF_TRACKER_ENTITY, "tracker"),
    (CONF_GPS_LOC, "gps"),
    (CONF_ZONE_ID, "zone"),
    (CONF_PROVINCE, "province"),
    (CONF_REGIONS, "regions"),
    (CONF_COUNTRY, "country"),
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CAPAlertsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    provider = entry.data.get(CONF_PROVIDER, "")
    options = entry.options
    resolved_config = coordinator.resolved_config
    resolved_options = coordinator.resolved_options
    alerts = list((coordinator.data or {}).values())
    active, upcoming = count_by_onset(alerts, datetime.now(timezone.utc))

    return {
        "entry": {
            "provider": provider,
            "platform_version": PLATFORM_VERSION,
            "scope": _scope(entry.data),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(options), TO_REDACT),
            # What the last update actually queried on, after tracker,
            # country-source and language resolution.
            "resolved": {
                "data": async_redact_data(dict(resolved_config), TO_REDACT),
                "options": async_redact_data(dict(resolved_options), TO_REDACT),
                "from_last_update": coordinator.last_update_success_time is not None,
            },
        },
        "source": {
            "endpoints": _endpoints(provider, resolved_config, resolved_options),
            "feed_source": (
                options.get(CONF_FEED_SOURCE, DEFAULT_FEED_SOURCE)
                if provider == "eccc"
                else None
            ),
        },
        "update": {
            "success": coordinator.last_update_success,
            "last_success": _iso(coordinator.last_update_success_time),
            "last_failure": _iso(coordinator.last_update_failure_time),
            "last_failure_error": coordinator.last_update_failure,
            "interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "timeout_seconds": options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        },
        "stream": {
            "enabled": coordinator.streaming,
            "connected": coordinator.stream_connected,
            "endpoint": (
                f"{NAAD_STREAM_HOST}:{NAAD_STREAM_PORT}"
                if coordinator.streaming
                else None
            ),
            "live_documents": coordinator.live_doc_count,
            "last_backfill": _iso(coordinator.last_backfill_time),
        },
        "filters": {
            "exclude_marine": options.get(CONF_EXCLUDE_MARINE, False),
            "geocode_prefixes": list(options.get(CONF_GEOCODE_PREFIXES) or []),
            "language": {
                "configured": options.get(CONF_LANGUAGE, "auto"),
                "resolved": resolved_options.get(CONF_LANGUAGE),
            },
        },
        "alerts": {
            "total": len(alerts),
            "active": active,
            "upcoming": upcoming,
            "by_severity": _by_severity(alerts),
            "truncated": max(0, len(alerts) - MAX_ALERT_ROWS),
            "entries": [_alert_row(a) for a in alerts[:MAX_ALERT_ROWS]],
        },
        "conventions": _conventions(provider, alerts),
    }


def _iso(value: datetime | None) -> str | None:
    """A timestamp as ISO 8601, or ``None``."""
    return value.isoformat() if value is not None else None


def _scope(data: Mapping[str, Any]) -> dict[str, Any]:
    """The location question this entry asks, as a mode and its value.

    The value comes from the redacted mapping, so a GPS-mode entry reports
    ``{"mode": "gps", "value": "**REDACTED**"}`` — the mode is the debuggable
    half, and it survives redaction.
    """
    redacted = async_redact_data(dict(data), TO_REDACT)
    for key, mode in _SCOPE_KEYS:
        if data.get(key):
            return {"mode": mode, "value": redacted.get(key)}
    # GDACS with no GPS filter is a fully configured worldwide entry, not a
    # half-configured one.
    return {"mode": "global", "value": None}


def _endpoints(
    provider: str, config: Mapping[str, Any], options: Mapping[str, Any]
) -> list[str]:
    """The upstream URLs this entry's next fetch would use.

    Built from the *resolved* config so a tracker entry names the endpoint it
    is actually polling. Only NWS puts the location into the URL, and there the
    coordinates are replaced rather than reproduced.
    """
    if provider == "nws":
        return _nws_endpoints(config)
    if provider == "eccc":
        return [url for _source, url in resolve_feed_urls(options)]
    if provider == "meteoalarm":
        slug = METEOALARM_COUNTRY_SLUGS.get((config.get(CONF_COUNTRY) or "").upper())
        return [METEOALARM_FEED_URL.format(country=slug)] if slug else []
    if provider == "wmo":
        source_id = (config.get(CONF_SOURCE_ID) or "").strip()
        return [WMO_RSS_URL.format(source_id=source_id)] if source_id else []
    if provider == "gdacs":
        # Both indexes, always — the union is not configurable.
        return [GDACS_RSS_CURRENT_URL, GDACS_RSS_24H_URL]
    return []


def _nws_endpoints(config: Mapping[str, Any]) -> list[str]:
    """NWS active-alerts and cancellation URLs, with any point scope redacted."""
    if config.get(CONF_ZONE_ID):
        scope = f"zone={config[CONF_ZONE_ID]}"
    elif config.get(CONF_GPS_LOC):
        scope = f"point={REDACTED}"
    else:
        return []
    return [
        f"{NWS_API_BASE}?{scope}",
        f"{NWS_ALL_BASE}?message_type=cancel&{scope}",
    ]


def _by_severity(alerts: Sequence[CAPAlert]) -> dict[str, int]:
    """Active-set headcount per normalized severity tier."""
    return dict(Counter(a.severity_normalized or "unknown" for a in alerts))


def _alert_row(alert: CAPAlert) -> dict[str, Any]:
    """One alert's identity and lifecycle — no body text, no geometry.

    ``geocodes`` are kept in full: "my prefix filter matches nothing" is only
    answerable against the codes the feed actually published, and they are no
    finer-grained than the zone or region the entry is already configured for.
    """
    return {
        "id": alert.id,
        "identifier": alert.identifier,
        "event": alert.event,
        "sender": alert.sender,
        "msg_type": alert.msg_type,
        "status": alert.status,
        "severity": alert.severity,
        "severity_normalized": alert.severity_normalized,
        "phase": alert.phase,
        "previous_phase": alert.previous_phase,
        "phase_changed": alert.phase_changed,
        "lifecycle_status": alert.lifecycle_status,
        "stale": alert.stale,
        "is_marine": alert.is_marine,
        "sent": alert.sent,
        "onset": alert.onset,
        "expires": alert.expires,
        "ends": alert.ends,
        "has_geometry": bool(alert.geometry_ref),
        "geocodes": {scheme: list(codes) for scheme, codes in alert.geocodes.items()},
    }


def _conventions(provider: str, alerts: Sequence[CAPAlert]) -> dict[str, Any]:
    """The convention rows in effect, and the senders that landed on each.

    The first question a per-sender dialect report raises is which row matched,
    and until now the only way to find out was to reproduce the fetch. Rows are
    keyed by source, so a MeteoAlarm entry relaying both MeteoFrance and a
    default-row member reports two of them.
    """
    senders_by_key: dict[str, set[str]] = {}
    for alert in alerts:
        key = _conventions_key(alert.provider, alert.sender)
        senders_by_key.setdefault(key, set()).add(alert.sender)
    rows = [
        {
            "key": key,
            "senders": sorted(s for s in senders if s),
            **_describe(CONVENTIONS.get(key, SourceConventions())),
        }
        for key, senders in sorted(senders_by_key.items())
    ]
    return {
        "provider_row": {
            "key": _conventions_key(provider, ""),
            **_describe(conventions_for(provider)),
        },
        "rows_in_effect": rows,
    }


def _conventions_key(provider: str, sender: str) -> str:
    """The table key ``conventions_for`` would resolve to, spelled out.

    ``conventions_for`` returns the row but not which key produced it, and the
    key is half the answer: "``meteoalarm``, not ``meteoalarm/vigilance@…``"
    is exactly what a MeteoFrance report needs to establish.
    """
    if sender and f"{provider}/{sender}" in CONVENTIONS:
        return f"{provider}/{sender}"
    if provider in CONVENTIONS:
        return provider
    return "(none)"


def _describe(conventions: SourceConventions) -> dict[str, Any]:
    """Render one convention row as JSON.

    Walks the dataclass rather than naming fields, so a rule added to the table
    shows up in the next dump without a change here.
    """
    described: dict[str, Any] = {
        f.name: _render(getattr(conventions, f.name)) for f in fields(conventions)
    }
    described["classifies_marine"] = conventions.classifies_marine
    return described


def _render(value: Any) -> Any:
    """One convention field as JSON — hooks by name, sets sorted, stages by slot."""
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    if isinstance(value, tuple):
        return [
            {"slot": stage.slot, "run": _callable_name(stage.run)}
            if isinstance(stage, PipelineStage)
            else _render(stage)
            for stage in value
        ]
    if isinstance(value, Mapping):
        return dict(value)
    if callable(value):
        return _callable_name(value)
    return value


def _callable_name(value: Any) -> str:
    """A hook's name, falling back to its repr for a lambda or a partial."""
    return getattr(value, "__name__", None) or repr(value)
