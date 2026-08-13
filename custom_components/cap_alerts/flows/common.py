"""Validators, schema helpers, and the title rule shared by every provider.

Anything here is used by two or more provider modules (or by the options
flow). Provider-specific validators and selectors live next to the steps that
render them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from ..const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ENTITY,
    CONF_GPS_LOC,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_REGION_LABELS,
    CONF_REGIONS,
    CONF_SOURCE_ID,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
    METEOALARM_COUNTRY_NAMES,
    WMO_SOURCE_NAMES,
)

_GPS_RE = re.compile(r"^-?\d+\.?\d*\s*,\s*-?\d+\.?\d*$")
# One area-code prefix. Deliberately not numeric-only: the filter compares
# against every geocode scheme a feed publishes, which includes alphabetic ones
# (NWS ``UGC`` "OHZ049", MeteoAlarm ``EMMA_ID`` "DE123"), so a digits-only rule
# would make the feature China-specific.
_GEOCODE_PREFIX_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,32}$")

# Provider-specific option fields are appended to the shared ones in this
# shape, keyed in render order.
OptionsSchema = dict[Any, Any]


def compute_scope_key(data: Mapping[str, Any]) -> str:
    """Canonical identity of the location an entry watches (issue #130).

    Used as the config entry's unique ID, so two entries asking the same
    upstream question abort as ``already_configured`` rather than doubling the
    polling, the devices and every alert entity.

    Every scope component present participates, rather than the first one
    matching: a WMO entry is a source *and* a location, a MeteoAlarm entry a
    country *and* a region set, and collapsing either to one component would
    call two genuinely different entries the same.

    Canonicalization is the whole job, since the same scope can be typed
    several ways. Multi-value fields are sorted (``OHC049,OHC035`` and
    ``OHC035,OHC049`` are one scope), and the single-value fields arrive
    already normalized: ``_validate_gps`` round-trips through ``float`` and
    ``_validate_zone`` upper-cases.

    Deliberately built from ``data`` alone. Behavior options — polling
    interval, language, marine exclusion, geocode prefixes — are not identity:
    they change after creation without changing which feed and area the entry
    watches, and the WMO setup flow writes its prefix straight to ``options``.
    (Issue #130 lists prefixes among the fields to sort; that predates their
    move out of entry data.)
    """
    provider = str(data.get(CONF_PROVIDER, "")).strip()
    parts: list[str] = [provider]

    def add(label: str, value: Any) -> None:
        if value:
            parts.extend((label, str(value).strip()))

    add("source", data.get(CONF_SOURCE_ID))
    add("country_source", data.get(CONF_COUNTRY_ENTITY))
    add("tracker", data.get(CONF_TRACKER_ENTITY))
    add("gps", data.get(CONF_GPS_LOC))
    zones = str(data.get(CONF_ZONE_ID, "") or "")
    add("zone", ",".join(sorted(z.strip() for z in zones.split(",") if z.strip())))
    add("province", data.get(CONF_PROVINCE))
    add("country", data.get(CONF_COUNTRY))
    regions = data.get(CONF_REGIONS) or ()
    add("regions", ",".join(sorted(str(r).strip() for r in regions)))

    # A worldwide GDACS entry has no scope component at all, and "everything"
    # is a scope: without this two of them would key on the bare provider,
    # which is the right answer for the wrong reason.
    if len(parts) == 1:
        parts.append("global")
    return ":".join(parts)


class ScopedEntryFlowMixin(ConfigFlow):
    """Create and update entries through the scope key, never around it.

    Every provider's steps go through these two helpers instead of calling
    ``async_create_entry`` / ``async_update_and_abort`` directly, so the
    duplicate check cannot be forgotten on one of the eighteen create paths —
    which is how the integration shipped without it at all.
    """

    async def _async_create_scoped_entry(
        self,
        data: dict[str, Any],
        options: Mapping[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create an entry, aborting if its scope is already configured.

        ``options`` is for the one mode that sets a behavior option at creation
        time (WMO's geocode narrowing); it takes no part in the key.
        """
        await self.async_set_unique_id(compute_scope_key(data))
        self._abort_if_unique_id_configured()
        if options is not None:
            return self.async_create_entry(
                title=_compute_device_title(data), data=data, options=options
            )
        return self.async_create_entry(title=_compute_device_title(data), data=data)

    async def _async_update_scoped_entry(
        self,
        entry: ConfigEntry,
        data: dict[str, Any],
        options: Mapping[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Rewrite an entry's scope, aborting if another entry already holds it.

        A reconfigure is *expected* to change the key, so the check has to
        exempt the entry being edited — ``_abort_if_unique_id_configured``
        would match it against itself and refuse every no-op edit.
        """
        key = compute_scope_key(data)
        await self.async_set_unique_id(key)
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(
            self.handler, key
        )
        if existing is not None and existing.entry_id != entry.entry_id:
            raise AbortFlow("already_configured")
        if options is not None:
            return self.async_update_and_abort(
                entry,
                unique_id=key,
                data=data,
                options=options,
                title=_compute_device_title(data),
            )
        return self.async_update_and_abort(
            entry,
            unique_id=key,
            data=data,
            title=_compute_device_title(data),
        )


def _tracker_schema(default: str | None = None) -> vol.Schema:
    """Schema with a single ``device_tracker`` entity selector.

    Shared by every provider's GPS-tracker step. ``default`` carries the
    current entity id forward in reconfigure flows.
    """
    if default is not None:
        key: Any = vol.Required(CONF_TRACKER_ENTITY, default=default)
    else:
        key = vol.Required(CONF_TRACKER_ENTITY)
    return vol.Schema(
        {key: EntitySelector(EntitySelectorConfig(domain="device_tracker"))}
    )


def _compute_device_title(data: dict[str, Any]) -> str:
    """Derive entry title from config data."""
    provider = data[CONF_PROVIDER].upper()
    if data[CONF_PROVIDER] == "wmo":
        source_id = data.get(CONF_SOURCE_ID, "unknown")
        source_name = WMO_SOURCE_NAMES.get(source_id, source_id)
        if CONF_GPS_LOC in data:
            location = f"{source_name} ({data[CONF_GPS_LOC]})"
        elif CONF_TRACKER_ENTITY in data:
            location = f"{source_name} ({data[CONF_TRACKER_ENTITY].split('.')[-1]})"
        else:
            location = source_name
    elif data[CONF_PROVIDER] == "gdacs":
        # "Global" is a real scope here, not a missing one — the GDACS index is
        # worldwide, so an entry with no GPS filter is fully configured and must
        # not fall through to the "Unknown" default.
        location = data.get(CONF_GPS_LOC, "Global")
    elif CONF_COUNTRY_ENTITY in data:
        # MeteoAlarm fully-mobile mode: country follows a source entity, so
        # there is no static location — surface the tracker name as "auto".
        location = f"auto: {data[CONF_TRACKER_ENTITY].split('.')[-1]}"
    elif CONF_ZONE_ID in data:
        location = data[CONF_ZONE_ID]
    elif CONF_GPS_LOC in data:
        location = data[CONF_GPS_LOC]
    elif CONF_TRACKER_ENTITY in data:
        location = data[CONF_TRACKER_ENTITY].split(".")[-1]
    elif CONF_PROVINCE in data:
        location = data[CONF_PROVINCE]
    elif CONF_REGIONS in data:
        country_code = data.get(CONF_COUNTRY, "")
        country_name = METEOALARM_COUNTRY_NAMES.get(country_code, country_code)
        labels = data.get(CONF_REGION_LABELS) or {}
        if labels:
            sorted_labels = sorted(labels.values())
            # Counted from the authoritative selection, not the label map:
            # a legacy entry may carry fewer labels than selected regions.
            extra = len(data[CONF_REGIONS]) - 1
            suffix = f" +{extra}" if extra > 0 else ""
            location = f"{country_code} — {sorted_labels[0]}{suffix}"
        else:
            count = len(data[CONF_REGIONS])
            location = f"{country_name} — {count} regions"
    elif CONF_COUNTRY in data:
        code = data[CONF_COUNTRY]
        location = METEOALARM_COUNTRY_NAMES.get(code, code)
    else:
        location = "Unknown"
    return f"CAP Alerts {provider} ({location})"


def _validate_gps(value: str) -> tuple[str, str | None]:
    """Validate GPS string. Returns (cleaned, error_key_or_None)."""
    if not _GPS_RE.match(value):
        return value, "invalid_gps"
    parts = value.split(",")
    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError:
        return value, "invalid_gps"
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return value, "invalid_gps"
    return f"{lat},{lon}", None


def _validate_geocode_prefixes(value: str) -> tuple[list[str], str | None]:
    """Parse a comma-separated area-code prefix list.

    Returns ``(prefixes, error_key_or_None)``. Empty input is *valid* and
    yields ``[]`` — this is an optional narrowing, and clearing the field is
    how a user turns it back off. Tokens are stripped, empties dropped, and
    duplicates collapsed order-preservingly. Stored verbatim rather than
    upper-cased so the user's input stays recognisable when the form is
    re-rendered; the filter casefolds at comparison time.
    """
    prefixes: list[str] = []
    for token in value.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        if not _GEOCODE_PREFIX_RE.match(cleaned):
            return [], "invalid_geocode_prefix"
        if cleaned not in prefixes:
            prefixes.append(cleaned)
    return prefixes, None
