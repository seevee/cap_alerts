"""Sensor entities for CAP Alerts: count, last updated, and per-alert entities."""

from __future__ import annotations

import hashlib
from collections.abc import Set as AbstractSet
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_PROVIDER, INCIDENT_DEVICE_CLASS, PLATFORM_VERSION
from .coordinator import AlertsDataUpdateCoordinator
from .model import CAPAlert
from .normalize import count_by_onset
from .payload import UNRECORDED_ATTRIBUTES, fit_to_budget

# Every entity on this platform reads from the coordinator's cached data and
# never polls upstream itself, so there is no request concurrency to cap.
PARALLEL_UPDATES = 0


def _short_hash(unique_id: str) -> str:
    """Return the 8-char SHA-1 prefix used to disambiguate entity IDs (RFC §2.2)."""
    return hashlib.sha1(unique_id.encode()).hexdigest()[:8]


def _alert_object_id(unique_id: str, event: str) -> str:
    """Build the collision-proof object_id for an alert entity."""
    return f"cap_alert_{slugify(event)}_{_short_hash(unique_id)}"


def _classify_sync(
    current_ids: set[str],
    tracked_ids: set[str],
    grace_ids: AbstractSet[str],
) -> tuple[set[str], set[str]]:
    """Compute (to_add, to_remove) sets for one coordinator cycle.

    `grace_ids` are tracked IDs hydrated from the registry that no fetch has
    settled yet; they are exempted from removal on this cycle.
    """
    to_add = current_ids - tracked_ids
    to_remove = (tracked_ids - current_ids) - grace_ids
    return to_add, to_remove


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up CAP Alerts sensor entities."""
    coordinator: AlertsDataUpdateCoordinator = entry.runtime_data

    # Static diagnostic sensors
    async_add_entities(
        [CountSensor(coordinator, entry), LastUpdatedSensor(coordinator, entry)]
    )

    # Dynamic alert entities
    tracked: dict[str, AlertEntity] = {}
    ent_reg = er.async_get(hass)

    # Hydrate tracked set from entity registry on startup and re-add them
    # to the platform so they can write state. Without this, hydrated
    # entities block creation of new entities for the same alert ID but
    # never become platform-registered, leaving them unavailable.
    provider = entry.data[CONF_PROVIDER]
    alert_prefix = f"{entry.entry_id}_{provider}_"
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if not ent.unique_id.startswith(alert_prefix):
            continue
        alert_id = ent.unique_id.removeprefix(alert_prefix)
        tracked[alert_id] = AlertEntity(coordinator, entry, alert_id)
    if tracked:
        async_add_entities(list(tracked.values()))

    @callback
    def _sync_alert_entities() -> None:
        alerts_by_id = coordinator.data or {}
        current_ids = set(alerts_by_id)
        tracked_ids = set(tracked)

        # Hydrated entities stay until the store settles them, which takes a
        # second fetch rather than a second update: a stream rebuild a minute
        # after boot can't recover what the seed backfill missed (#252).
        to_add, to_remove = _classify_sync(
            current_ids, tracked_ids, coordinator.boot_pending_ids
        )

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
            removed: AlertEntity | None = tracked.pop(alert_id, None)
            if removed is None:
                continue
            if ent_reg.async_get(removed.entity_id):
                ent_reg.async_remove(removed.entity_id)

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
        return self.coordinator.device_info


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

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Break the total down into alerts in force now vs. still to start.

        The state stays the total (issue #99): templates already key off it,
        so the breakdown rides along as attributes rather than changing what
        the number means. Recomputed on read against the wall clock, so an
        upcoming alert crosses over at the next poll rather than waiting for
        the feed to restate it.
        """
        alerts = list((self.coordinator.data or {}).values())
        active, upcoming = count_by_onset(alerts, datetime.now(timezone.utc))
        return {"active": active, "upcoming": upcoming}


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
    # Not a ``SensorDeviceClass`` member: core sets custom classes aside before
    # it validates, which ``tests/test_device_class_binding.py`` pins (#277).
    _attr_device_class = INCIDENT_DEVICE_CLASS  # type: ignore[assignment]
    # Keeps the providers' verbatim ``<parameter>`` catch-all (#150) and the
    # per-area geocode container (#245) out of history — and, because the
    # recorder measures its ceiling against the recorded set, out of the
    # attribute budget as well. Both stay on the live state.
    _unrecorded_attributes = UNRECORDED_ATTRIBUTES

    def __init__(
        self,
        coordinator: AlertsDataUpdateCoordinator,
        entry: ConfigEntry,
        alert_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._alert_id = alert_id
        self._entry = entry
        provider = entry.data[CONF_PROVIDER]
        self._attr_unique_id = f"{entry.entry_id}_{provider}_{alert_id}"

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    async def async_added_to_hass(self) -> None:
        """Release the store's parked events once this entity is in place.

        ``incident_created`` for a first sighting waits in the store until the
        entity it names exists (issue #249). Home Assistant writes the first
        state synchronously right after this method returns, so the release is
        scheduled one loop turn out rather than called here: a ``@callback``
        bus listener that reads ``states.get(entity_id)`` on the event then
        finds the alert's attributes, which is what a kiosk pop-up wants. An
        entity restored from the registry at boot has nothing parked, so for
        it the call is a no-op.
        """
        await super().async_added_to_hass()
        self.hass.loop.call_soon(
            self.coordinator.async_release_events, self._alert_id, self.entity_id
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
        uid = self.unique_id
        if not a or not a.event or not uid:
            return None
        return _alert_object_id(uid, a.event)

    @property
    def native_value(self) -> str | None:
        a = self._alert
        return a.severity_normalized if a else None

    @property
    def icon(self) -> str | None:
        a = self._alert
        return a.icon or None if a else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Sparse CAP attributes, trimmed to what the recorder will store.

        The trim runs here rather than in ``normalize`` so the ``CAPAlert``
        keeps the full text the source sent: ``store.process()`` diffs against
        that, and a platform-induced truncation would otherwise show up in
        ``changed_fields`` as if the feed had reworded the alert.
        """
        a = self._alert
        if not a:
            return {}
        attrs = a.to_attributes()
        attrs["incident_platform_version"] = PLATFORM_VERSION
        return fit_to_budget(attrs)
