"""Automation triggers over the incident events (issue #280).

Five triggers for the Add trigger dialog: an alert issued, updated, escalated,
downgraded, ended. Each listens for one of the ``incident_*`` bus events, keeps
the target's alert entities current through core's registry tracker, and
compares severity on the normalized rank with a threshold the user picks.

Built on the events rather than on core's entity-state helper: that helper
compares a new state with a previous one, and an alert that arrives severe
has no previous state. The store already tells a first sighting from an
escalation from a restart re-validation, and says so on the bus, so the
triggers take the store's word. Core's calendar triggers are the precedent,
a target plus an options schema the integration owns.

The target tracker's ``async_setup`` is a coroutine on current releases and a
plain method on the 2026.4.3 floor, and the floor's ``async_attach_runner``
takes one argument where later releases pass two. Both are handled here so
one file runs on every release the integration supports.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, cast

import voluptuous as vol
from homeassistant.const import CONF_OPTIONS, CONF_TARGET
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.target import TargetEntityChangeTracker, TargetSelection
from homeassistant.helpers.trigger import Trigger, TriggerActionRunner, TriggerConfig
from homeassistant.helpers.typing import ConfigType

from .const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)
from .normalize import SEVERITY_RANK

CONF_THRESHOLD = "threshold"
THRESHOLD_ANY = "any"
THRESHOLD_DEFAULT = "severe"
_ANY_RANK = SEVERITY_RANK["unknown"]

# The select's options, on the severity axis the store ranks by. ``any`` takes
# ``unknown``'s rung at the bottom, so an authority that publishes no severity
# can still fire a trigger set to it.
THRESHOLD_RANK: Mapping[str, int] = {
    (THRESHOLD_ANY if severity == "unknown" else severity): rank
    for severity, rank in SEVERITY_RANK.items()
}

_TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TARGET): cv.TARGET_FIELDS,
        vol.Required(CONF_OPTIONS, default={}): {
            vol.Required(CONF_THRESHOLD, default=THRESHOLD_DEFAULT): vol.In(
                list(THRESHOLD_RANK)
            ),
        },
    }
)

_Matcher = Callable[[Mapping[str, Any], int], bool]


def _rank(severity: Any) -> int:
    """The rank of a payload's severity token; anything unrecognised is the floor."""
    return SEVERITY_RANK.get(severity, 0) if isinstance(severity, str) else 0


def _at_or_above(data: Mapping[str, Any], threshold: int) -> bool:
    return _rank(data.get("severity")) >= threshold


def _severity_move(data: Mapping[str, Any]) -> tuple[int, int] | None:
    """The (previous, current) ranks of an update that changed severity, else None."""
    if "severity_normalized" not in (data.get("changed_fields") or ()):
        return None
    return _rank(data.get("previous_severity")), _rank(data.get("severity"))


def _crossed_up(data: Mapping[str, Any], threshold: int) -> bool:
    """Severity rose from under the threshold to at or over it.

    Severe to extreme with the threshold at severe is not a crossing: the alert
    was already what the trigger asks about. ``any`` has nothing under it, so
    there it means every rise, ``unknown`` to ``minor`` included.
    """
    if (move := _severity_move(data)) is None:
        return False
    previous, current = move
    if threshold == _ANY_RANK:
        return current > previous
    return previous < threshold <= current


def _crossed_down(data: Mapping[str, Any], threshold: int) -> bool:
    """Severity fell from at or over the threshold to under it: the mirror.

    Extreme to severe with the threshold at severe is not a crossing, the alert
    is still what the trigger asks about. ``any`` means every fall. Measured on
    the dev box recorder, one severity change in five goes this way: fire tiers
    stepping down, a cyclone bulletin easing, a flood advisory's final revision.
    """
    if (move := _severity_move(data)) is None:
        return False
    previous, current = move
    if threshold == _ANY_RANK:
        return current < previous
    return current < threshold <= previous


def _alert_entities(entities: set[str]) -> set[str]:
    """Keep only the sensors of a target's expansion.

    A device target also expands to the location's button and binary sensor.
    The incident events only ever name alert sensors, so the domain is the
    only filter the match needs; ``triggers.yaml`` narrows the picker further.
    """
    return {
        entity_id for entity_id in entities if split_entity_id(entity_id)[0] == "sensor"
    }


class _IncidentEventListener(TargetEntityChangeTracker):
    """Fire the runner for the target's alerts when their event passes the threshold."""

    def __init__(
        self,
        hass: HomeAssistant,
        target_selection: TargetSelection,
        event_type: str,
        matches: _Matcher,
        threshold: int,
        run_action: TriggerActionRunner,
    ) -> None:
        super().__init__(hass, target_selection, _alert_entities)
        self._event_type = event_type
        self._threshold = threshold
        self._matches = matches
        self._run_action = run_action
        self._tracked: set[str] = set()
        self._bus_unsub: CALLBACK_TYPE | None = None

    async def async_setup(self) -> CALLBACK_TYPE:
        """Track the target and listen on the bus; returns the unsubscribe."""
        setup = super().async_setup()
        if inspect.isawaitable(setup):
            await setup
        self._bus_unsub = self._hass.bus.async_listen(
            self._event_type, self._handle_event
        )
        return self._unsubscribe

    @callback
    def _handle_entities_update(self, tracked_entities: set[str]) -> None:
        self._tracked = tracked_entities

    @callback
    def _handle_event(self, event: Event[Any]) -> None:
        data = event.data
        if data.get("entity_id") not in self._tracked or not self._matches(
            data, self._threshold
        ):
            return
        # The event's own context, so the automation trace leads back to the
        # cycle that fired it (issue #310).
        self._run_action(dict(data), f"{self._event_type} event", event.context)

    def _unsubscribe(self) -> None:
        super()._unsubscribe()
        if self._bus_unsub is not None:
            self._bus_unsub()
            self._bus_unsub = None


class _IncidentTrigger(Trigger):
    """A trigger bound to one incident event, with a target and a threshold.

    ``_matches`` reads the event's payload against the threshold's rank. Two
    triggers share ``incident_updated``, one for each direction, so the matcher
    is the class's and not the event's.
    """

    _event_type: str
    _matches: _Matcher

    @classmethod
    async def async_validate_config(
        cls, hass: HomeAssistant, config: ConfigType
    ) -> ConfigType:
        return cast(ConfigType, _TRIGGER_SCHEMA(config))

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        super().__init__(hass, config)
        self._target: dict[str, Any] = config.target or {}
        self._threshold = THRESHOLD_RANK[(config.options or {})[CONF_THRESHOLD]]

    async def async_attach_runner(
        self, run_action: TriggerActionRunner, did_not_trigger: Any = None
    ) -> CALLBACK_TYPE:
        target_selection = TargetSelection(self._target)
        if not target_selection.has_any_target:
            raise HomeAssistantError(f"No target defined in {self._target}")
        listener = _IncidentEventListener(
            self._hass,
            target_selection,
            self._event_type,
            self._matches,
            self._threshold,
            run_action,
        )
        return await listener.async_setup()


class IncidentArrivedTrigger(_IncidentTrigger):
    """A new alert at or above the threshold."""

    _event_type = EVENT_INCIDENT_CREATED
    _matches = staticmethod(_at_or_above)


class IncidentUpdatedTrigger(_IncidentTrigger):
    """Any revision of a known alert at or above the threshold.

    Every revision, an extended expiry included; ``changed_fields`` is on the
    trigger variables for a condition to narrow it.
    """

    _event_type = EVENT_INCIDENT_UPDATED
    _matches = staticmethod(_at_or_above)


class IncidentEscalatedTrigger(_IncidentTrigger):
    """A known alert whose severity crossed up to the threshold."""

    _event_type = EVENT_INCIDENT_UPDATED
    _matches = staticmethod(_crossed_up)


class IncidentDowngradedTrigger(_IncidentTrigger):
    """A known alert whose severity crossed down below the threshold."""

    _event_type = EVENT_INCIDENT_UPDATED
    _matches = staticmethod(_crossed_down)


class IncidentEndedTrigger(_IncidentTrigger):
    """An alert at or above the threshold ending."""

    _event_type = EVENT_INCIDENT_REMOVED
    _matches = staticmethod(_at_or_above)


TRIGGERS: dict[str, type[Trigger]] = {
    "incident_arrived": IncidentArrivedTrigger,
    "incident_updated": IncidentUpdatedTrigger,
    "incident_escalated": IncidentEscalatedTrigger,
    "incident_downgraded": IncidentDowngradedTrigger,
    "incident_ended": IncidentEndedTrigger,
}


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    """Return the triggers for alert entities."""
    return TRIGGERS
