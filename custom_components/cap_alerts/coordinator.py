"""DataUpdateCoordinator for CAP Alerts."""

from __future__ import annotations

import asyncio
import logging
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
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_TRACKER_ENTITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .geometry_store import GeometryStore
from .model import CAPAlert
from .normalize import normalize_alerts
from .providers import AlertProvider
from .store import AlertStore

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self._provider = provider
        self._store = AlertStore(hass, entry.entry_id, provider.name)
        self._geometry_store = geometry_store
        self._user_agent = user_agent
        self._timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        self.last_update_success_time: datetime | None = None

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
        - Language "auto": resolves to concrete "en-CA" or "fr-CA".
        """
        config = dict(self.config_entry.data)
        options = dict(self.config_entry.options)

        # Resolve tracker entity -> GPS coordinates
        if CONF_TRACKER_ENTITY in config:
            state = self.hass.states.get(config[CONF_TRACKER_ENTITY])
            if state and state.attributes.get(ATTR_LATITUDE):
                config[CONF_GPS_LOC] = (
                    f"{state.attributes[ATTR_LATITUDE]},"
                    f"{state.attributes[ATTR_LONGITUDE]}"
                )
            else:
                config[CONF_GPS_LOC] = ""

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
        alerts = normalize_alerts(alerts)
        # Externalize geometry for alerts that will remain active. Skipping
        # terminal-phase alerts avoids caching polygons we're about to drop.
        active_refs: set[str] = set()
        for a in alerts:
            if a.phase in ("cancel", "expired"):
                continue
            if a.geometry_ref and a.geometry:
                await self._geometry_store.put(a.geometry_ref, a.geometry)
                active_refs.add(a.geometry_ref)
        await self._geometry_store.purge_missing(
            active_refs, prefix=f"{self._provider.name}:"
        )
        # Diff against previous poll — returns only active alerts.
        alerts = self._store.process(alerts)
        # Track successful update time (not all HA versions expose this)
        self.last_update_success_time = datetime.now(timezone.utc)
        # Index by ID for O(1) lookup
        return {a.id: a for a in alerts}
