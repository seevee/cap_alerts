"""CAP Alerts — one entity per active weather alert."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .const import (
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    PLATFORMS,
    USER_AGENT,
)
from .coordinator import AlertsDataUpdateCoordinator
from .geometry_store import GeometryStore
from .providers import get_provider
from .views import CapAlertsGeometryView
from .websocket import async_register as async_register_ws

type CAPAlertsConfigEntry = ConfigEntry[AlertsDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CAPAlertsConfigEntry) -> bool:
    """Set up CAP Alerts from a config entry."""
    instance_id = await async_get_instance_id(hass)
    user_agent = USER_AGENT.format(instance_id)

    domain_data = hass.data.setdefault(DOMAIN, {})
    if "geometry_store" not in domain_data:
        store = GeometryStore(hass)
        await store.load_once()
        domain_data["geometry_store"] = store
    if not domain_data.get("registered"):
        hass.http.register_view(CapAlertsGeometryView(domain_data["geometry_store"]))
        async_register_ws(hass)
        domain_data["registered"] = True

    provider = get_provider(entry.data[CONF_PROVIDER])
    coordinator = AlertsDataUpdateCoordinator(
        hass, entry, provider, user_agent, domain_data["geometry_store"]
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
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
    coordinator.update_interval = timedelta(
        seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    coordinator.update_timeout(entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))
    await coordinator.async_request_refresh()
