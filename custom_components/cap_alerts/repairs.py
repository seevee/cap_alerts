"""Fix flows for the repairs raised in ``issues.py`` (issue #163).

Each issue recommends a single option value, and writing it is something the
integration can do itself, so every card gets a confirm flow that applies the
recommendation on Submit. A user who chose the flagged configuration on
purpose — polling because the socket is blocked, say — has Home Assistant's
own *Ignore* on the card; the step text says so.

Nothing here closes the card: the repairs flow manager deletes a fixable
issue when its flow completes, and the options write fires the entry update
listener, which re-syncs (a no-op by then) and either reloads the entry (the
streaming toggle) or refreshes it in place (``feed_source``, read per fetch).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_FEED_SOURCE,
    CONF_STREAMING,
    DEFAULT_FEED_SOURCE,
    ISSUE_ECCC_FEED_SOURCE_PELMOREX,
    ISSUE_ECCC_STREAMING_OFF,
)

# What Submit writes, per issue stem.
_FIXES: dict[str, tuple[str, Any]] = {
    ISSUE_ECCC_STREAMING_OFF: (CONF_STREAMING, True),
    ISSUE_ECCC_FEED_SOURCE_PELMOREX: (CONF_FEED_SOURCE, DEFAULT_FEED_SOURCE),
}


class OptionRepairFlow(RepairsFlow):
    """Confirm, then write one option on the entry."""

    def __init__(self, entry: ConfigEntry, key: str, value: Any) -> None:
        self._entry = entry
        self._key = key
        self._value = value
        super().__init__()

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self._entry,
                options={**self._entry.options, self._key: self._value},
            )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"title": self._entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for one issue."""
    entry = None
    if data and isinstance(data.get("entry_id"), str):
        entry = hass.config_entries.async_get_entry(str(data["entry_id"]))
    for stem, (key, value) in _FIXES.items():
        if not issue_id.startswith(f"{stem}_"):
            continue
        if entry is None:
            # Removed while the card was still open: nothing to write, and
            # the confirm flow's Submit just closes the issue.
            return ConfirmRepairFlow()
        return OptionRepairFlow(entry, key, value)
    raise ValueError(f"unknown repair {issue_id}")
