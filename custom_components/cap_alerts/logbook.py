"""Describe the incident events for the logbook, so Activity shows them (#310).

Logbook keeps a state row only when the old state and the new one both exist
and differ, so an alert entity's first state and its removal never show. The
``incident_*`` events are the record of that lifecycle, and logbook records an
event type only once its integration has described it. This platform is that
description; logbook discovers it by filename, there is nothing to declare in
the manifest.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.components.logbook.const import (
    LOGBOOK_ENTRY_CONTEXT_ID,
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.components.logbook.models import LazyEventPartialState
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_PROVIDER,
    DOMAIN,
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
    REMOVAL_REASON_ENDED,
    REMOVAL_REASON_SUPERSEDED,
)
from .coordinator import device_name

# ``changed_fields`` names model fields; the one that isn't also the CAP name
# reads better as the attribute it drives.
_FIELD_LABELS = {"severity_normalized": "severity"}


def describe_name(hass: HomeAssistant, data: Mapping[str, Any]) -> str:
    """The row's subject, shaped like the alert entity's friendly name.

    Home Assistant prefixes the device name onto an entity that sets
    ``has_entity_name``, so a state row for the alert reads "CAP Alerts NWS
    Winter Storm Warning changed to severe"; the event rows match. The entry
    can be gone by the time a row is read, in which case the event stands
    alone, and the event can be blank on a removal announced from the
    registry (see events.md), in which case the device name does.
    """
    parts: list[str] = []
    entry = hass.config_entries.async_get_entry(data.get("entry_id") or "")
    if entry is not None and (provider := entry.data.get(CONF_PROVIDER)):
        parts.append(device_name(provider))
    if event := data.get("event"):
        parts.append(event)
    return " ".join(parts) or "CAP Alerts"


def describe_message(event_type: str, data: Mapping[str, Any]) -> str:
    """What happened, as the rest of the row after the name."""
    if event_type == EVENT_INCIDENT_CREATED:
        severity = data.get("severity")
        if severity and severity != "unknown":
            return f"issued ({severity})"
        return "issued"
    if event_type == EVENT_INCIDENT_UPDATED:
        fields = [_FIELD_LABELS.get(f, f) for f in data.get("changed_fields") or []]
        if fields:
            return f"updated ({', '.join(fields)})"
        return "updated"
    reason = data.get("removal_reason")
    if reason == REMOVAL_REASON_SUPERSEDED:
        return "superseded"
    if reason == REMOVAL_REASON_ENDED:
        return "ended"
    return "cancelled" if data.get("phase") == "cancel" else "expired"


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[LazyEventPartialState], dict[str, Any]]], None
    ],
) -> None:
    """Teach logbook the three incident events."""

    def describer(
        event_type: str,
    ) -> Callable[[LazyEventPartialState], dict[str, Any]]:
        @callback
        def describe(event: LazyEventPartialState) -> dict[str, Any]:
            data = event.data
            return {
                LOGBOOK_ENTRY_NAME: describe_name(hass, data),
                LOGBOOK_ENTRY_MESSAGE: describe_message(event_type, data),
                # Links the row to the alert entity, and puts it on that
                # entity's own Activity list.
                LOGBOOK_ENTRY_ENTITY_ID: data.get("entity_id"),
                LOGBOOK_ENTRY_CONTEXT_ID: event.context_id,
            }

        return describe

    for event_type in (
        EVENT_INCIDENT_CREATED,
        EVENT_INCIDENT_UPDATED,
        EVENT_INCIDENT_REMOVED,
    ):
        async_describe_event(DOMAIN, event_type, describer(event_type))
