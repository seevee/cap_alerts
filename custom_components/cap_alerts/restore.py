"""Alert restore store — the live set, persisted per config entry (issue #281).

Alert entities do not inherit ``RestoreEntity``, so after a restart the store
knows a registry id but not its content, and with no ``expires`` to retain
against it removes the id after two fetches that lack it. On a gappy index
(ECCC omits live alerts in about half of samples) that is a false all-clear on
boot, then a fresh ``incident_created`` when the index recovers.

This module persists the active set so the store can be seeded with content.
It uses its own ``Store`` rather than ``RestoreEntity``: core keeps a removed
RestoreEntity's state for seven days and rewrites the whole restore file every
15 minutes, which on a busy entry is 1–1.7 MB per write
(docs/evidence/restoreentity-keeps-a-removed-alert-for-seven-days.md). This
file is written when the set changes, coalesced over ``RESTORE_SAVE_DELAY``,
and once at unload or shutdown, which is what RFC §1.4 requirement 5 asks of
``.storage/`` writes. A quiet entry never creates the file at all.

``geometry`` is not persisted (RFC §2.4: polygons are ephemeral, the next
fetch refills them) and neither is the per-reconciliation transition metadata.
Everything else on ``CAPAlert`` round-trips, including fields added later.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, RESTORE_SAVE_DELAY, RESTORE_STORAGE_VERSION
from .model import CAPAlert, geocodes_from

_LOGGER = logging.getLogger(__name__)

# Fields that never reach disk: ``geometry`` is refilled by the next fetch, and
# the two transition markers describe one reconciliation, not the alert.
EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {"geometry", "previous_phase", "phase_changed"}
)

# Every persisted field, and the subset whose JSON lists must come back as
# tuples. Read from the model's resolved type hints (a ``tuple[...]`` or a
# ``tuple[...] | None``), so a new tuple field round-trips without an edit here.
_PERSISTED: tuple[str, ...] = tuple(
    f.name for f in fields(CAPAlert) if f.name not in EXCLUDED_FIELDS
)


def _is_tuple_hint(hint: Any) -> bool:
    origin = get_origin(hint)
    if origin is tuple:
        return True
    return origin in (Union, UnionType) and any(
        get_origin(arg) is tuple for arg in get_args(hint)
    )


_TUPLE_FIELDS: frozenset[str] = frozenset(
    name for name, hint in get_type_hints(CAPAlert).items() if _is_tuple_hint(hint)
)

# What a load can raise on a file this code does not understand: a shape it
# cannot walk, a record ``CAPAlert`` rejects, or a newer major version the Store
# has no migration for. Logged and treated as no file, never raised.
_LOAD_ERRORS = (
    HomeAssistantError,
    NotImplementedError,
    AttributeError,
    KeyError,
    TypeError,
    ValueError,
)


def _to_json(value: Any) -> Any:
    """Tuples to lists and mappings to dicts, at any depth."""
    if isinstance(value, Mapping):
        return {key: _to_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_json(item) for item in value]
    return value


def _to_tuple(value: Any) -> Any:
    """Lists to tuples at any depth. The loaded JSON is fresh, so dicts pass through."""
    if isinstance(value, list):
        return tuple(_to_tuple(item) for item in value)
    return value


def alert_to_storage(alert: CAPAlert) -> dict[str, Any]:
    """Serialize every persisted field to JSON-safe values.

    Unlike ``to_attributes`` nothing empty is dropped: this is for an exact
    round trip, not a sparse payload.
    """
    return {name: _to_json(getattr(alert, name)) for name in _PERSISTED}


def alert_from_storage(data: Mapping[str, Any]) -> CAPAlert:
    """Rebuild a ``CAPAlert`` from ``alert_to_storage`` output.

    Unknown keys are ignored and missing ones take the dataclass default, so a
    file written before or after a model change still loads. Raises
    ``TypeError`` when ``id`` is missing.
    """
    kwargs: dict[str, Any] = {}
    for name in _PERSISTED:
        if name not in data:
            continue
        value = data[name]
        if name == "geocodes":
            # The one special case: the container is an immutable mapping built
            # through the model's own funnel, not a plain tuple or dict.
            kwargs[name] = geocodes_from(value)
        elif name in _TUPLE_FIELDS and value is not None:
            kwargs[name] = _to_tuple(value)
        else:
            kwargs[name] = value
    return CAPAlert(**kwargs)


@dataclass(frozen=True, slots=True)
class RestoredSet:
    """The alert set a previous run left on disk."""

    alerts: tuple[CAPAlert, ...]
    scope_key: str | None
    # ISO 8601 of the reconciliation that last confirmed the set: a lower bound
    # on when each alert was last known live.
    confirmed_at: str


def _payload(alerts: Iterable[CAPAlert]) -> list[dict[str, Any]]:
    return [alert_to_storage(a) for a in sorted(alerts, key=lambda a: a.id)]


class AlertRestoreStore:
    """One Store per config entry at ``.storage/cap_alerts.<entry_id>``."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Bind the entry's storage file; nothing is read until ``async_load``."""
        self._store: Store[dict[str, Any]] = Store(
            hass, RESTORE_STORAGE_VERSION, f"{DOMAIN}.{entry_id}", atomic_writes=True
        )
        # The alert list last loaded or written, for change detection. ``None``
        # means no file has been seen or written, which compares as empty.
        self._saved: list[dict[str, Any]] | None = None
        # True once a file exists on disk, readable or not.
        self._exists = False
        self._last_saved: str | None = None

    @property
    def last_saved(self) -> str | None:
        """ISO time of the last write this process scheduled or made."""
        return self._last_saved

    async def async_load(self) -> RestoredSet | None:
        """Load the saved set; ``None`` when there is none or it is unreadable."""
        try:
            raw = await self._store.async_load()
            if raw is None:
                return None
            self._exists = True
            records = raw["alerts"]
            alerts = tuple(alert_from_storage(record) for record in records)
            restored = RestoredSet(
                alerts=alerts,
                scope_key=raw.get("scope_key"),
                confirmed_at=str(raw["confirmed_at"]),
            )
        except _LOAD_ERRORS as err:
            _LOGGER.warning(
                "Discarding unreadable alert restore file %s: %s",
                self._store.key,
                err,
            )
            return None
        self._saved = _payload(alerts)
        return restored

    @callback
    def async_save_if_changed(
        self,
        alerts: Iterable[CAPAlert],
        *,
        scope_key: str | None,
        confirmed_at: str,
    ) -> bool:
        """Schedule a delayed write if the alert list moved; return whether it did.

        Only the alerts are compared. A fresh ``confirmed_at`` alone is not a
        reason to write; unload and shutdown refresh it via ``async_save_now``.
        """
        payload = _payload(alerts)
        if payload == (self._saved or []):
            return False
        data = _document(payload, scope_key, confirmed_at)
        self._store.async_delay_save(lambda: data, RESTORE_SAVE_DELAY)
        self._remember(payload)
        return True

    async def async_save_now(
        self,
        alerts: Iterable[CAPAlert],
        *,
        scope_key: str | None,
        confirmed_at: str,
    ) -> None:
        """Write immediately, for unload and HA stop, so ``confirmed_at`` is fresh.

        Skipped when the set is empty and no file exists, so a quiet entry does
        not create one at shutdown.
        """
        payload = _payload(alerts)
        if not payload and self._saved is None and not self._exists:
            return
        await self._store.async_save(_document(payload, scope_key, confirmed_at))
        self._remember(payload)

    async def async_remove(self) -> None:
        """Delete the file, for an entry being removed."""
        await self._store.async_remove()
        self._saved = None
        self._exists = False

    def _remember(self, payload: list[dict[str, Any]]) -> None:
        self._saved = payload
        self._exists = True
        self._last_saved = dt_util.utcnow().isoformat()


def _document(
    payload: list[dict[str, Any]], scope_key: str | None, confirmed_at: str
) -> dict[str, Any]:
    return {"scope_key": scope_key, "confirmed_at": confirmed_at, "alerts": payload}
