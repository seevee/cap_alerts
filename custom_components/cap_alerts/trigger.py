"""Automation triggers over the incident events (issue #280).

Five triggers for the Add trigger dialog: an alert issued, updated, escalated,
downgraded, ended. Each listens for one of the ``incident_*`` bus events, keeps
the target's alert entities current through core's registry tracker, and
compares severity on the normalized rank with a threshold the user picks.

One more is timed rather than listened for (issue #326): an alert starts.
Arrival runs ahead of onset on four in ten ECCC alerts and most NWS ones, by
half a day typically, so "issued" is the wrong moment for a kiosk. It
schedules a timer per targeted alert on its ``onset`` attribute, with an
offset before or after, re-armed whenever a revision moves the time and fired
once per value. The listener takes the attribute as a parameter; ``expires``
was tried and dropped, since CAP ``expires`` is the message's validity and
ECCC and WMO extend it on nearly every revision, so "before it expires" fired
ahead of re-issues.

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

import abc
import inspect
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from functools import partial
from typing import Any, cast

import voluptuous as vol
from homeassistant.const import CONF_OFFSET, CONF_OPTIONS, CONF_TARGET
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HassJob,
    HomeAssistant,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.helpers.target import TargetEntityChangeTracker, TargetSelection
from homeassistant.helpers.trigger import Trigger, TriggerActionRunner, TriggerConfig
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)
from .normalize import SEVERITY_RANK

CONF_THRESHOLD = "threshold"
CONF_OFFSET_TYPE = "offset_type"
OFFSET_BEFORE = "before"
OFFSET_AFTER = "after"
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

_THRESHOLD_FIELD = {
    vol.Required(CONF_THRESHOLD, default=THRESHOLD_DEFAULT): vol.In(
        list(THRESHOLD_RANK)
    ),
}

_TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TARGET): cv.TARGET_FIELDS,
        vol.Required(CONF_OPTIONS, default={}): _THRESHOLD_FIELD,
    }
)

_TIMED_TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TARGET): cv.TARGET_FIELDS,
        vol.Required(CONF_OPTIONS, default={}): {
            **_THRESHOLD_FIELD,
            vol.Required(CONF_OFFSET, default=timedelta(0)): cv.time_period,
            vol.Required(CONF_OFFSET_TYPE, default=OFFSET_BEFORE): vol.In(
                [OFFSET_BEFORE, OFFSET_AFTER]
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


class _AlertTracker(TargetEntityChangeTracker):
    """Keep a target's alert entities current for a listener to act on.

    The tracker's ``async_setup`` is a coroutine on current core and a plain
    method on the 2026.4.3 floor, so it is awaited only when it needs to be.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        target_selection: TargetSelection,
        run_action: TriggerActionRunner,
    ) -> None:
        super().__init__(hass, target_selection, _alert_entities)
        self._run_action = run_action
        self._tracked: set[str] = set()

    async def async_setup(self) -> CALLBACK_TYPE:
        """Track the target and subscribe; returns the unsubscribe."""
        setup = super().async_setup()
        if inspect.isawaitable(setup):
            await setup
        self._subscribe()
        return self._unsubscribe

    @callback
    def _subscribe(self) -> None:
        """Whatever the listener needs beyond the registry subscriptions."""


class _IncidentEventListener(_AlertTracker):
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
        super().__init__(hass, target_selection, run_action)
        self._event_type = event_type
        self._threshold = threshold
        self._matches = matches
        self._bus_unsub: CALLBACK_TYPE | None = None

    @callback
    def _subscribe(self) -> None:
        self._bus_unsub = self._hass.bus.async_listen(
            self._event_type, self._handle_event
        )

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


class _TimedListener(_AlertTracker):
    """Fire the runner when a targeted alert's timestamp attribute arrives.

    One timer per alert, keyed on the attribute's value: a revision that moves
    the time re-arms, a revision that leaves it alone keeps the timer, and a
    value that has already fired never fires again. A time already past when
    the alert is first seen does not fire, "issued" covered that. The
    threshold is read at firing, against the severity the alert has then.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        target_selection: TargetSelection,
        attribute: str,
        threshold: int,
        offset: timedelta,
        run_action: TriggerActionRunner,
    ) -> None:
        super().__init__(hass, target_selection, run_action)
        self._attribute = attribute
        self._threshold = threshold
        self._offset = offset
        self._timers: dict[str, tuple[datetime, CALLBACK_TYPE]] = {}
        self._fired: dict[str, str] = {}
        self._state_unsub: CALLBACK_TYPE | None = None

    @callback
    def _handle_entities_update(self, tracked_entities: set[str]) -> None:
        for entity_id in self._tracked - tracked_entities:
            self._disarm(entity_id)
            self._fired.pop(entity_id, None)
        self._tracked = tracked_entities
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        if tracked_entities:
            self._state_unsub = async_track_state_change_event(
                self._hass, list(tracked_entities), self._handle_state_change
            )
        for entity_id in tracked_entities:
            self._arm(entity_id)

    @callback
    def _handle_state_change(self, event: Event[Any]) -> None:
        self._arm(event.data["entity_id"])

    @callback
    def _arm(self, entity_id: str) -> None:
        state = self._hass.states.get(entity_id)
        value = state.attributes.get(self._attribute) if state else None
        if (
            not isinstance(value, str)
            or (when := _parse_time(value)) is None
            or value == self._fired.get(entity_id)
        ):
            self._disarm(entity_id)
            return
        fire_at = when + self._offset
        armed = self._timers.get(entity_id)
        if armed is not None and armed[0] == fire_at:
            return
        self._disarm(entity_id)
        if fire_at <= dt_util.utcnow():
            return
        unsub = async_track_point_in_time(
            self._hass, HassJob(partial(self._fire, entity_id, value)), fire_at
        )
        self._timers[entity_id] = (fire_at, unsub)

    @callback
    def _disarm(self, entity_id: str) -> None:
        armed = self._timers.pop(entity_id, None)
        if armed is not None:
            armed[1]()

    @callback
    def _fire(self, entity_id: str, value: str, _now: datetime) -> None:
        self._timers.pop(entity_id, None)
        state = self._hass.states.get(entity_id)
        if state is None or state.attributes.get(self._attribute) != value:
            return
        self._fired[entity_id] = value
        if _rank(state.state) < self._threshold:
            return
        attrs = state.attributes
        payload = {
            "entity_id": entity_id,
            "incident_id": attrs.get("id"),
            "event": attrs.get("event"),
            "severity": state.state,
            "area_desc": attrs.get("area_desc"),
            self._attribute: value,
            CONF_OFFSET: self._offset,
        }
        self._run_action(payload, f"alert {self._attribute} reached")

    def _unsubscribe(self) -> None:
        super()._unsubscribe()
        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None
        for entity_id in list(self._timers):
            self._disarm(entity_id)


def _parse_time(value: str) -> datetime | None:
    """A CAP timestamp attribute as an aware UTC datetime, or None."""
    when = dt_util.parse_datetime(value) if value else None
    return dt_util.as_utc(when) if when is not None else None


class _TargetedTrigger(Trigger):
    """A trigger on a target of alert entities, with a severity threshold.

    Subclasses name their options schema and build the listener; the target
    check and the attach are the same for all seven.
    """

    _schema: vol.Schema

    @classmethod
    async def async_validate_config(
        cls, hass: HomeAssistant, config: ConfigType
    ) -> ConfigType:
        return cast(ConfigType, cls._schema(config))

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        super().__init__(hass, config)
        self._target: dict[str, Any] = config.target or {}
        self._options: dict[str, Any] = config.options or {}
        self._threshold = THRESHOLD_RANK[self._options[CONF_THRESHOLD]]

    @abc.abstractmethod
    def _listener(
        self, target_selection: TargetSelection, run_action: TriggerActionRunner
    ) -> _AlertTracker:
        """The tracker that fires ``run_action`` for this trigger."""

    async def async_attach_runner(
        self, run_action: TriggerActionRunner, did_not_trigger: Any = None
    ) -> CALLBACK_TYPE:
        target_selection = TargetSelection(self._target)
        if not target_selection.has_any_target:
            raise HomeAssistantError(f"No target defined in {self._target}")
        return await self._listener(target_selection, run_action).async_setup()


class _IncidentTrigger(_TargetedTrigger):
    """A trigger bound to one incident event.

    ``_matches`` reads the event's payload against the threshold's rank. Two
    triggers share ``incident_updated``, one for each direction, so the matcher
    is the class's and not the event's.
    """

    _schema = _TRIGGER_SCHEMA
    _event_type: str
    _matches: _Matcher

    def _listener(
        self, target_selection: TargetSelection, run_action: TriggerActionRunner
    ) -> _AlertTracker:
        return _IncidentEventListener(
            self._hass,
            target_selection,
            self._event_type,
            self._matches,
            self._threshold,
            run_action,
        )


class _TimedIncidentTrigger(_TargetedTrigger):
    """A trigger on a targeted alert's timestamp attribute, with an offset."""

    _schema = _TIMED_TRIGGER_SCHEMA
    _attribute: str

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        super().__init__(hass, config)
        offset: timedelta = self._options[CONF_OFFSET]
        before = self._options[CONF_OFFSET_TYPE] == OFFSET_BEFORE
        self._offset = -offset if before else offset

    def _listener(
        self, target_selection: TargetSelection, run_action: TriggerActionRunner
    ) -> _AlertTracker:
        return _TimedListener(
            self._hass,
            target_selection,
            self._attribute,
            self._threshold,
            self._offset,
            run_action,
        )


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


class IncidentStartedTrigger(_TimedIncidentTrigger):
    """A targeted alert's onset arrives, offset before or after."""

    _attribute = "onset"


TRIGGERS: dict[str, type[Trigger]] = {
    "incident_arrived": IncidentArrivedTrigger,
    "incident_updated": IncidentUpdatedTrigger,
    "incident_escalated": IncidentEscalatedTrigger,
    "incident_downgraded": IncidentDowngradedTrigger,
    "incident_ended": IncidentEndedTrigger,
    "incident_started": IncidentStartedTrigger,
}


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    """Return the triggers for alert entities."""
    return TRIGGERS
