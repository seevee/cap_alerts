"""Sensor entities for CAP Alerts: count, last updated, and per-alert entities."""

from __future__ import annotations

import hashlib
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_PROVIDER, DOMAIN, PLATFORM_VERSION
from .coordinator import AlertsDataUpdateCoordinator
from .model import CAPAlert


def _short_hash(unique_id: str) -> str:
    """Return the 8-char SHA-1 prefix used to disambiguate entity IDs (RFC §2.2.1)."""
    return hashlib.sha1(unique_id.encode()).hexdigest()[:8]


def _alert_object_id(unique_id: str, event: str) -> str:
    """Build the collision-proof object_id for an alert entity."""
    return f"cap_alert_{slugify(event)}_{_short_hash(unique_id)}"


def _classify_sync(
    current_ids: set[str],
    tracked_ids: set[str],
    grace_ids: set[str],
) -> tuple[set[str], set[str]]:
    """Compute (to_add, to_remove) sets for one coordinator cycle.

    `grace_ids` are tracked IDs hydrated from the registry that have not yet
    appeared in coordinator data; they are exempted from removal on this cycle.
    Caller is responsible for clearing `grace_ids` after this cycle.
    """
    to_add = current_ids - tracked_ids
    to_remove = (tracked_ids - current_ids) - grace_ids
    return to_add, to_remove


async def async_setup_entry(
    hass,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up CAP Alerts sensor entities."""
    coordinator: AlertsDataUpdateCoordinator = entry.runtime_data

    # Static diagnostic sensors
    async_add_entities([CountSensor(coordinator, entry), LastUpdatedSensor(coordinator, entry)])

    # Dynamic alert entities
    tracked: dict[str, AlertEntity] = {}
    grace_ids: set[str] = set()
    ent_reg = er.async_get(hass)

    # Hydrate tracked set from entity registry on startup and re-add them
    # to the platform so they can write state. Without this, hydrated
    # entities block creation of new entities for the same alert ID but
    # never become platform-registered, leaving them unavailable.
    provider = entry.data.get(CONF_PROVIDER, "nws")
    alert_prefix = f"{entry.entry_id}_{provider}_"
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if not ent.unique_id.startswith(alert_prefix):
            continue
        alert_id = ent.unique_id.removeprefix(alert_prefix)
        tracked[alert_id] = AlertEntity(coordinator, entry, alert_id)
        grace_ids.add(alert_id)
    if tracked:
        async_add_entities(list(tracked.values()))

    first_sync = True

    @callback
    def _sync_alert_entities() -> None:
        nonlocal first_sync
        alerts_by_id = coordinator.data or {}
        current_ids = set(alerts_by_id)
        tracked_ids = set(tracked)

        active_grace = grace_ids if first_sync else set()
        to_add, to_remove = _classify_sync(current_ids, tracked_ids, active_grace)

        # Additions: batched single call
        if to_add:
            new_entities: list[AlertEntity] = []
            for alert_id in to_add:
                entity = AlertEntity(coordinator, entry, alert_id)
                tracked[alert_id] = entity
                new_entities.append(entity)
            async_add_entities(new_entities)

        # Removals: idempotent — check the registry before calling async_remove
        for alert_id in to_remove:
            entity = tracked.pop(alert_id, None)
            if entity is None:
                continue
            if ent_reg.async_get(entity.entity_id):
                ent_reg.async_remove(entity.entity_id)

        if first_sync:
            grace_ids.clear()
            first_sync = False

    unsub = coordinator.async_add_listener(_sync_alert_entities)
    entry.async_on_unload(unsub)
    _sync_alert_entities()


class _CAPAlertsEntity(CoordinatorEntity[AlertsDataUpdateCoordinator], SensorEntity):
    """Base class for CAP Alerts entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AlertsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="CAP Alerts",
            model=self.coordinator.provider.name.upper(),
        )


class CountSensor(_CAPAlertsEntity):
    """Sensor showing the number of active alerts."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "alert_count"

    def __init__(
        self,
        coordinator: AlertsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_count"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data or {})


class LastUpdatedSensor(_CAPAlertsEntity):
    """Sensor showing the last successful update time."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_updated"

    def __init__(
        self,
        coordinator: AlertsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_updated"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_update_success_time


class AlertEntity(CoordinatorEntity[AlertsDataUpdateCoordinator], SensorEntity):
    """Sensor representing a single active weather alert."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AlertsDataUpdateCoordinator,
        entry: ConfigEntry,
        alert_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._alert_id = alert_id
        self._entry = entry
        provider = entry.data.get(CONF_PROVIDER, "nws")
        self._attr_unique_id = f"{entry.entry_id}_{provider}_{alert_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="CAP Alerts",
            model=self.coordinator.provider.name.upper(),
        )

    @property
    def _alert(self) -> CAPAlert | None:
        alerts = self.coordinator.data or {}
        return alerts.get(self._alert_id)

    @property
    def name(self) -> str | None:
        a = self._alert
        return a.event if a else None

    @property
    def suggested_object_id(self) -> str | None:
        a = self._alert
        if not a or not a.event:
            return None
        return _alert_object_id(self.unique_id, a.event)

    @property
    def native_value(self) -> str | None:
        a = self._alert
        return a.severity_normalized if a else None

    @property
    def icon(self) -> str | None:
        a = self._alert
        return a.icon or None if a else None

    @property
    def extra_state_attributes(self) -> dict:
        a = self._alert
        if not a:
            return {}
        attrs = a.to_attributes()
        attrs["incident_platform_version"] = PLATFORM_VERSION
        return attrs
