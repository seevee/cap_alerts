"""Alert store — inter-poll diffing, transition detection, HA event firing."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.core import HomeAssistant

from .model import CAPAlert


class AlertStore:
    """Tracks alert state across poll cycles for transition detection."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
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
                self._fire_event("cap_alert_created", updated)
            elif prev.phase != alert.phase:
                updated = replace(
                    alert,
                    previous_phase=prev.phase,
                    phase_changed=True,
                )
                self._fire_event("cap_alert_updated", updated)
            else:
                updated = replace(alert, previous_phase=prev.phase)
            result.append(updated)

        # Detect removed alerts
        for alert_id, prev in self._previous.items():
            if alert_id not in current:
                self._fire_event("cap_alert_removed", prev)

        self._previous = current
        return result

    def _fire_event(self, event_type: str, alert: CAPAlert) -> None:
        """Fire an HA event for automation consumption."""
        self._hass.bus.async_fire(
            event_type,
            {
                "entry_id": self._entry_id,
                "alert_id": alert.id,
                "event": alert.event,
                "phase": alert.phase,
                "previous_phase": alert.previous_phase,
                "severity": alert.severity_normalized,
                "area_desc": alert.area_desc,
            },
        )
