"""Restart restore of the lifecycle fields, one file per config entry (issue #350).

Alert entities do not inherit ``RestoreEntity``, so after a restart the store
knows a registry id but nothing of what it was. A removal announced from the
registry carries ``severity: unknown`` and no area, which the threshold
triggers (issue #280) rank below every threshold, and the absence rule
(RFC §2.5) has no ``expires`` to retain against.

This module keeps the few fields those two consumers read, per live alert, in
a ``Store`` file per entry: ``store.RECORD_FIELDS``, keyed by alert id, with
the scope key and the time of the last confirming reconciliation beside them.
Not the content. Headline, description and geometry are refilled by the first
fetch, and persisting them is what RFC §2.5 argues against (#281, #289).
Stock ``RestoreEntity`` is not used because core keeps a removed entity's
state for seven days and rewrites the whole restore file per dump
(docs/evidence/restoreentity-keeps-a-removed-alert-for-seven-days.md).

Written when a reconciliation changed the set, coalesced over
``RESTORE_SAVE_DELAY``, and once at unload and at shutdown so ``confirmed_at``
is the last reconciliation rather than the last change. A quiet entry never
creates the file at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, RESTORE_SAVE_DELAY, RESTORE_STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# What a load can raise on a file this code does not understand: a shape it
# cannot walk, or a newer major version the Store has no migration for. Logged
# and treated as no file, never raised: a lost restore costs one thin removal
# per alert, a failed setup costs the entry.
_LOAD_ERRORS = (
    HomeAssistantError,
    NotImplementedError,
    AttributeError,
    KeyError,
    TypeError,
    ValueError,
)

Records = dict[str, dict[str, str]]


@dataclass(frozen=True, slots=True)
class RestoredSet:
    """What a previous run left on disk."""

    # alert id → the persisted fields of that alert.
    records: Records
    scope_key: str | None
    # ISO 8601 of the reconciliation that last confirmed the set: a lower bound
    # on when each alert was last known live.
    confirmed_at: str


class AlertRestoreStore:
    """One Store per config entry at ``.storage/cap_alerts.<entry_id>``."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Bind the entry's storage file; nothing is read until ``async_load``."""
        self._store: Store[dict[str, Any]] = Store(
            hass, RESTORE_STORAGE_VERSION, f"{DOMAIN}.{entry_id}", atomic_writes=True
        )
        # The records last loaded or written, for change detection. ``None``
        # means no file has been seen or written, which compares as empty.
        self._saved: Records | None = None
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
            records: Records = {
                str(alert_id): {str(k): str(v) for k, v in record.items()}
                for alert_id, record in raw["alerts"].items()
            }
            restored = RestoredSet(
                records=records,
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
        self._saved = records
        return restored

    @callback
    def async_save_if_changed(
        self, records: Records, *, scope_key: str | None, confirmed_at: str
    ) -> bool:
        """Schedule a delayed write if the records moved; return whether they did.

        Only the records are compared. A fresh ``confirmed_at`` alone is not a
        reason to write; unload and shutdown refresh it via ``async_save_now``.
        """
        if records == (self._saved or {}):
            return False
        data = _document(records, scope_key, confirmed_at)
        self._store.async_delay_save(lambda: data, RESTORE_SAVE_DELAY)
        self._remember(records)
        return True

    async def async_save_now(
        self, records: Records, *, scope_key: str | None, confirmed_at: str
    ) -> None:
        """Write immediately, for unload and HA stop, so ``confirmed_at`` is fresh.

        Skipped when the set is empty and no file exists, so a quiet entry does
        not create one at shutdown.
        """
        if not records and self._saved is None and not self._exists:
            return
        await self._store.async_save(_document(records, scope_key, confirmed_at))
        self._remember(records)

    async def async_remove(self) -> None:
        """Delete the file, for an entry being removed."""
        await self._store.async_remove()
        self._saved = None
        self._exists = False

    def _remember(self, records: Records) -> None:
        self._saved = records
        self._exists = True
        self._last_saved = dt_util.utcnow().isoformat()


def _document(
    records: Records, scope_key: str | None, confirmed_at: str
) -> dict[str, Any]:
    return {"scope_key": scope_key, "confirmed_at": confirmed_at, "alerts": records}
