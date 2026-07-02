"""DataUpdateCoordinator for CAP Alerts."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ATTRIBUTE,
    CONF_COUNTRY_ENTITY,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_TRACKER_ENTITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    METEOALARM_COUNTRIES,
    METEOALARM_COUNTRY_CODE_ALIASES,
    METEOALARM_COUNTRY_NAME_ALIASES,
    METEOALARM_COUNTRY_NAMES,
)
from .geometry_store import GeometryStore
from .model import CAPAlert
from .normalize import normalize_alerts
from .providers import AlertProvider
from .providers.cap_content_cache import CAPContentCache
from .store import AlertStore

_LOGGER = logging.getLogger(__name__)


def _resolve_tracker_gps(state: Any) -> str | None:
    """Resolve a ``device_tracker`` state to a ``"lat,lon"`` string.

    Returns ``None`` when the state is missing or carries no usable
    coordinates. Latitude/longitude of exactly ``0.0`` is valid — only a
    truly absent attribute (``None``) is treated as unresolvable, so the
    equator/prime-meridian are not silently dropped.
    """
    if state is None:
        return None
    lat = state.attributes.get(ATTR_LATITUDE)
    lon = state.attributes.get(ATTR_LONGITUDE)
    if lat is None or lon is None:
        return None
    return f"{lat},{lon}"


def _resolve_country_code(value: Any) -> str | None:
    """Map a country-source value to a MeteoAlarm ISO-2 code, or ``None``.

    Accepts a two-letter code — MeteoAlarm's own (``"UK"``), ISO 3166-1
    (``"GB"``), or the EU institutional variant (``"EL"``) — or a country
    name, case-insensitively. Names match ``METEOALARM_COUNTRY_NAMES``
    display names and ``METEOALARM_COUNTRY_NAME_ALIASES``, after stripping
    parenthetical suffixes so reverse-geocoder output like
    ``"Moldova (the Republic of)"`` resolves. Non-string or unrecognized
    values return ``None``.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    upper = cleaned.upper()
    if upper in METEOALARM_COUNTRIES:
        return upper
    if upper in METEOALARM_COUNTRY_CODE_ALIASES:
        return METEOALARM_COUNTRY_CODE_ALIASES[upper]
    folded = re.sub(r"\s*\([^)]*\)", "", cleaned).strip().casefold()
    alias = METEOALARM_COUNTRY_NAME_ALIASES.get(folded)
    if alias is not None:
        return alias
    for iso, name in METEOALARM_COUNTRY_NAMES.items():
        if name.casefold() == folded:
            return iso
    return None


class AlertsDataUpdateCoordinator(DataUpdateCoordinator[dict[str, CAPAlert]]):
    """Coordinator that delegates fetching to a provider."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass,
        entry: ConfigEntry,
        provider: AlertProvider,
        user_agent: str,
        geometry_store: GeometryStore,
        cap_content_cache: CAPContentCache | None = None,
    ) -> None:
        self._provider = provider
        self._store = AlertStore(hass, entry.entry_id, provider.name)
        self._geometry_store = geometry_store
        self._user_agent = user_agent
        self._cap_content_cache = cap_content_cache
        self._timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        self.last_update_success_time: datetime | None = None
        # Guard a single warning per failure streak when a tracker or
        # MeteoAlarm country-source entity can't be resolved, so the
        # per-poll resolution doesn't spam the log.
        self._tracker_resolve_warned = False
        self._country_resolve_warned = False

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )

    @property
    def provider(self) -> AlertProvider:
        """Expose provider for device_info model field."""
        return self._provider

    def update_timeout(self, timeout: int) -> None:
        """Called by options update listener."""
        self._timeout = timeout

    def _resolve_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve config and options before passing to provider.

        - Tracker mode: resolves tracker entity -> lat/lon coordinates.
        - Country-source mode: resolves a country entity -> ISO-2 country.
        - Language "auto": resolves to concrete "en-CA" or "fr-CA".
        """
        config = dict(self.config_entry.data)
        options = dict(self.config_entry.options)

        # Resolve tracker entity -> GPS coordinates. An unresolvable tracker
        # (missing state or no lat/lon) raises UpdateFailed so the entry goes
        # visibly unavailable rather than silently degrading to zero or
        # country-wide alerts.
        if CONF_TRACKER_ENTITY in config:
            entity_id = config[CONF_TRACKER_ENTITY]
            gps = _resolve_tracker_gps(self.hass.states.get(entity_id))
            if gps is None:
                provider = config.get(CONF_PROVIDER, "")
                if not self._tracker_resolve_warned:
                    _LOGGER.warning(
                        "%s: tracker %s has no location", provider, entity_id
                    )
                    self._tracker_resolve_warned = True
                raise UpdateFailed(f"{provider}: tracker {entity_id} has no location")
            self._tracker_resolve_warned = False
            config[CONF_GPS_LOC] = gps

        # Resolve country-source entity -> ISO-2 country (MeteoAlarm mobile
        # mode). Leaving CONF_COUNTRY unset lets the provider's existing
        # "country not configured" path surface UpdateFailed.
        if CONF_COUNTRY_ENTITY in config:
            entity_id = config[CONF_COUNTRY_ENTITY]
            state = self.hass.states.get(entity_id)
            value: str | None = None
            if state is not None and state.state not in (
                "",
                "unknown",
                "unavailable",
            ):
                attr = config.get(CONF_COUNTRY_ATTRIBUTE)
                value = state.attributes.get(attr) if attr else state.state
            code = _resolve_country_code(value)
            if code is None:
                if not self._country_resolve_warned:
                    _LOGGER.warning(
                        "MeteoAlarm: could not resolve country from %s (value=%r)",
                        entity_id,
                        value,
                    )
                    self._country_resolve_warned = True
            else:
                self._country_resolve_warned = False
                config[CONF_COUNTRY] = code

        # Resolve language "auto" -> concrete code. ECCC is bilingual EN/FR;
        # MeteoAlarm spans ~35 locales — pass the 2-letter prefix of
        # hass.config.language so the provider's language-prefix matcher
        # finds the closest <cap:info> block.
        lang = options.get(CONF_LANGUAGE, "auto")
        if lang == "auto":
            provider = config.get(CONF_PROVIDER, "")
            if provider == "meteoalarm":
                options[CONF_LANGUAGE] = (
                    self.hass.config.language.split("-", 1)[0].lower() or "en"
                )
            else:
                options[CONF_LANGUAGE] = (
                    "fr-CA" if self.hass.config.language.startswith("fr") else "en-CA"
                )

        return config, options

    async def _async_update_data(self) -> dict[str, CAPAlert]:
        config, options = self._resolve_config()
        try:
            async with asyncio.timeout(self._timeout):
                alerts = await self._provider.async_fetch(
                    async_get_clientsession(self.hass),
                    config,
                    options,
                    cap_content_cache=self._cap_content_cache,
                    user_agent=self._user_agent,
                )
        except TimeoutError as err:
            raise UpdateFailed(
                f"{self._provider.name}: timeout after {self._timeout}s"
            ) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"{self._provider.name}: {err}") from err

        # Shared normalization. The full normalized list — including
        # cancelled/expired alerts — is handed to store.process so it can
        # fire cap_alert_removed with the true terminal phase before
        # dropping them from the active set (RFC §2.3).
        entry_id = self.config_entry.entry_id
        alerts = normalize_alerts(alerts, entry_id)
        # Externalize geometry for alerts that will remain active. Skipping
        # terminal-phase alerts avoids caching polygons we're about to drop.
        active_refs: set[str] = set()
        for a in alerts:
            if a.phase in ("cancel", "expired"):
                continue
            if a.geometry_ref and a.geometry:
                await self._geometry_store.put(a.geometry_ref, a.geometry)
                active_refs.add(a.geometry_ref)
        # Purge only this entry's refs (geometry_ref is entry-namespaced), so a
        # sibling entry on the same provider keeps its geometry.
        await self._geometry_store.purge_missing(active_refs, prefix=f"{entry_id}:")
        # Diff against previous poll — returns only active alerts.
        alerts = self._store.process(alerts)
        # Track successful update time (not all HA versions expose this)
        self.last_update_success_time = datetime.now(timezone.utc)
        # Index by ID for O(1) lookup
        return {a.id: a for a in alerts}
