"""Alert store — inter-poll diffing, transition detection, HA event firing."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)
from .model import CAPAlert

# Fields whose changes automations typically care about. Anything outside
# this allowlist (normalized timestamps, parameters dict, geometry, etc.)
# would be noise in ``changed_fields``.
CHANGED_FIELDS_ALLOWLIST: tuple[str, ...] = (
    "headline",
    "description",
    "instruction",
    "severity_normalized",
    "phase",
    "expires",
    "area_desc",
)


class AlertStore:
    """Tracks alert state across poll cycles for transition detection."""

    def __init__(self, hass: HomeAssistant, entry_id: str, provider: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._provider = provider
        self._previous: dict[str, CAPAlert] = {}

    def process(self, alerts: list[CAPAlert]) -> list[CAPAlert]:
        """Diff incoming alerts against previous poll.

        Accepts the *unfiltered* normalized list so terminal-phase alerts
        (``cancel``/``expired``) can fire ``incident_removed`` with their
        true terminal phase (RFC §2.3) before being dropped from the
        returned active set. Alerts that disappear silently between polls
        are inferred as ``expired`` if their ``expires`` timestamp is in
        the past, otherwise ``cancel``.

        Returns only the active alerts (``phase`` ∈ ``{new, update}``),
        with ``previous_phase`` and ``phase_changed`` set.
        """
        incoming = {a.id: a for a in alerts}
        active: dict[str, CAPAlert] = {}
        result: list[CAPAlert] = []

        for alert_id, alert in incoming.items():
            prev = self._previous.get(alert_id)
            terminal = alert.phase in ("cancel", "expired")

            if prev is None:
                updated = replace(alert, phase_changed=True)
                if terminal:
                    # First sight is already terminal — emit removed only.
                    self._fire_event(
                        EVENT_INCIDENT_REMOVED,
                        updated,
                        phase_changed=True,
                        changed_fields=[],
                    )
                    continue
                self._fire_event(
                    EVENT_INCIDENT_CREATED,
                    updated,
                    phase_changed=True,
                    changed_fields=[],
                )
            else:
                changed = _diff_fields(prev, alert)
                phase_changed = prev.phase != alert.phase
                updated = replace(
                    alert,
                    previous_phase=prev.phase,
                    phase_changed=phase_changed,
                )
                if terminal:
                    self._fire_event(
                        EVENT_INCIDENT_REMOVED,
                        updated,
                        phase_changed=phase_changed,
                        changed_fields=changed,
                    )
                    continue
                if changed:
                    self._fire_event(
                        EVENT_INCIDENT_UPDATED,
                        updated,
                        phase_changed=phase_changed,
                        changed_fields=changed,
                    )
            active[alert_id] = updated
            result.append(updated)

        # Silent disappearance: provider dropped the alert without a Cancel
        # message. Infer the terminal phase from the expires timestamp.
        now = datetime.now(timezone.utc)
        for alert_id, prev in self._previous.items():
            if alert_id in incoming:
                continue
            inferred = _infer_terminal_phase(prev, now)
            terminal_alert = replace(
                prev,
                previous_phase=prev.phase,
                phase=inferred,
                phase_changed=prev.phase != inferred,
            )
            self._fire_event(
                EVENT_INCIDENT_REMOVED,
                terminal_alert,
                phase_changed=terminal_alert.phase_changed,
                changed_fields=[],
            )

        self._previous = active
        return result

    def _fire_event(
        self,
        event_type: str,
        alert: CAPAlert,
        *,
        phase_changed: bool,
        changed_fields: list[str],
    ) -> None:
        """Fire an HA event matching RFC §2.3 (schema documented in docs/events.md).

        ``entry_id`` and ``area_desc`` are project extensions not in the RFC.
        """
        payload: dict = {
            "entry_id": self._entry_id,
            "incident_id": alert.id,
            "event": alert.event,
            "severity": alert.severity_normalized,
            "phase": alert.phase,
            "phase_changed": phase_changed,
            "changed_fields": changed_fields,
            "area_desc": alert.area_desc,
        }
        # entity_id: look up via entity registry by unique_id.
        # On first sighting the entity isn't registered yet; omit the key.
        unique_id = f"{self._entry_id}_{self._provider}_{alert.id}"
        ent_reg = er.async_get(self._hass)
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is not None:
            payload["entity_id"] = entity_id

        self._hass.bus.async_fire(event_type, payload)


def _diff_fields(prev: CAPAlert, curr: CAPAlert) -> list[str]:
    """Return allowlisted field names whose values differ between prev/curr."""
    return [
        name
        for name in CHANGED_FIELDS_ALLOWLIST
        if getattr(prev, name) != getattr(curr, name)
    ]


def _infer_terminal_phase(alert: CAPAlert, now: datetime) -> str:
    """Infer a terminal phase for an alert that vanished between polls.

    ``expired`` if the alert's ``expires`` timestamp is in the past,
    otherwise ``cancel`` (the provider dropped the record without a Cancel
    message — treat as an implicit cancel).
    """
    expires_at = _parse_iso(alert.expires)
    if expires_at is not None and now >= expires_at:
        return "expired"
    return "cancel"


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp; return ``None`` on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
