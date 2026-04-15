"""Alert store — inter-poll diffing, transition detection, HA event firing."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
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

        Returns alerts with previous_phase and phase_changed set.
        Fires HA events for transitions.
        """
        current = {a.id: a for a in alerts}
        result: list[CAPAlert] = []

        for alert_id, alert in current.items():
            prev = self._previous.get(alert_id)
            if prev is None:
                updated = replace(alert, phase_changed=True)
                self._fire_event(
                    "cap_alert_created",
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
                if changed:
                    self._fire_event(
                        "cap_alert_updated",
                        updated,
                        phase_changed=phase_changed,
                        changed_fields=changed,
                    )
            result.append(updated)

        # Detect removed alerts
        for alert_id, prev in self._previous.items():
            if alert_id not in current:
                self._fire_event(
                    "cap_alert_removed",
                    prev,
                    phase_changed=False,
                    changed_fields=[],
                )

        self._previous = current
        return result

    def _fire_event(
        self,
        event_type: str,
        alert: CAPAlert,
        *,
        phase_changed: bool,
        changed_fields: list[str],
    ) -> None:
        """Fire an HA event matching RFC §2.2.2."""
        payload: dict = {
            "entry_id": self._entry_id,
            "incident_id": alert.id,
            "alert_id": alert.id,  # deprecated alias; keep for existing consumers
            "event": alert.event,
            "severity": alert.severity_normalized,
            "phase": alert.phase,
            "phase_changed": phase_changed,
            "changed_fields": changed_fields,
            "area_desc": alert.area_desc,
        }
        # RFC §2.2.2 entity_id: look up via entity registry by unique_id.
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
