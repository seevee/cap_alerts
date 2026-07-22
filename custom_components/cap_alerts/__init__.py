"""CAP Alerts — one entity per active weather alert."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .const import (
    CONF_PROVIDER,
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    PLATFORMS,
    USER_AGENT,
)
from .coordinator import AlertsDataUpdateCoordinator
from .geometry_store import GeometryStore
from .providers import get_provider
from .providers.cap_content_cache import CAPContentCache
from .views import CapAlertsGeometryView
from .websocket import async_register as async_register_ws

type CAPAlertsConfigEntry = ConfigEntry[AlertsDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CAPAlertsConfigEntry) -> bool:
    """Set up CAP Alerts from a config entry."""
    instance_id = await async_get_instance_id(hass)
    user_agent = USER_AGENT.format(instance_id)

    domain_data = hass.data.setdefault(DOMAIN, {})
    if "geometry_store" not in domain_data:
        domain_data["geometry_store"] = GeometryStore()
    if "cap_content_cache" not in domain_data:
        domain_data["cap_content_cache"] = CAPContentCache()
    if not domain_data.get("registered"):
        hass.http.register_view(CapAlertsGeometryView(domain_data["geometry_store"]))
        async_register_ws(hass)
        domain_data["registered"] = True

    provider = get_provider(entry.data[CONF_PROVIDER])
    coordinator = AlertsDataUpdateCoordinator(
        hass,
        entry,
        provider,
        user_agent,
        domain_data["geometry_store"],
        domain_data["cap_content_cache"],
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Start the NAAD real-time stream (no-op unless ECCC streaming is enabled).
    # The first refresh above already seeded the active set from the GeoRSS
    # backfill, so the stream only needs to carry live updates + reconnect gaps.
    await coordinator.async_start_stream()
    entry.async_on_unload(coordinator.async_stop_stream)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CAPAlertsConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(
    hass: HomeAssistant, entry: CAPAlertsConfigEntry
) -> None:
    """Apply options changes without reloading."""
    coordinator: AlertsDataUpdateCoordinator = entry.runtime_data
    # Toggling real-time streaming changes ingestion wiring captured when the
    # coordinator was built (the stream task, the poll-vs-resync interval), so a
    # clean reload is simpler and safer than in-place re-wiring.
    if AlertsDataUpdateCoordinator._streaming_enabled(entry) != coordinator._streaming:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    coordinator.update_interval = coordinator._resolve_update_interval(entry)
    coordinator.update_timeout(entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))
    await coordinator.async_request_refresh()
