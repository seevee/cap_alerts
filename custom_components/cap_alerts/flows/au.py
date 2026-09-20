"""Australian state feeds: setup, reconfigure, and options steps (issue #127)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import (
    AU_ALERT_LEVEL_ALL,
    AU_ALERT_LEVELS,
    AU_STATE_LABELS,
    CONF_ALERT_LEVEL,
    CONF_PROVIDER,
    CONF_PROVINCE,
)
from .common import OptionsSchema, ScopedEntryFlowMixin


def _state_selector() -> SelectSelector:
    """Dropdown of the states with a CAP-AU feed, in ``AU_STATE_LABELS`` order."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=code, label=label)
                for code, label in AU_STATE_LABELS.items()
            ],
            mode=SelectSelectorMode.DROPDOWN,
            sort=False,
        )
    )


def _state_schema(default: str | None = None) -> vol.Schema:
    """Schema with the single state dropdown.

    Stored under ``CONF_PROVINCE``, the existing sub-national scope key, so
    the scope key, the entry title and diagnostics need no new case.
    """
    if default is not None:
        key: Any = vol.Required(CONF_PROVINCE, default=default)
    else:
        key = vol.Required(CONF_PROVINCE)
    return vol.Schema({key: _state_selector()})


def options_schema(entry: ConfigEntry) -> OptionsSchema:
    """AU-specific option fields: the minimum Australian Warning System tier.

    ``All`` (the default) keeps everything the feed publishes, including the
    agencies' informational tiers below the ladder (planned burns, incidents
    with no warning attached). Applied after parsing — there is nothing to
    save upstream, the feed is one document either way.
    """
    return {
        vol.Optional(
            CONF_ALERT_LEVEL,
            default=entry.options.get(CONF_ALERT_LEVEL, AU_ALERT_LEVEL_ALL),
        ): vol.In([AU_ALERT_LEVEL_ALL, *AU_ALERT_LEVELS]),
    }


class AUFlowMixin(ScopedEntryFlowMixin):
    """AU steps, mixed into the domain's flow handler."""

    # ── AU setup ──

    async def async_step_au(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """AU location scope menu.

        One mode only: a state. There is no GPS or tracker mode because a
        point can only be tested against polygons, and most NSW and QLD
        alerts carry a location marker and no polygon — a GPS scope would
        silently drop the majority of a feed. The card's radius filter over
        ``points`` is the intended way to narrow a state feed to a home.
        """
        return self.async_show_menu(
            step_id="au",
            # "user" is the back edge (issue #140); see the NWS menu.
            menu_options=["au_state", "user"],
        )

    async def async_step_au_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # No validator: the dropdown is closed, so the schema itself rejects
        # anything outside ``AU_STATE_LABELS`` before the step sees it.
        if user_input is not None:
            data = {CONF_PROVIDER: "au", CONF_PROVINCE: user_input[CONF_PROVINCE]}
            return await self._async_create_scoped_entry(data)
        return self.async_show_form(step_id="au_state", data_schema=_state_schema())

    # ── AU reconfigure ──

    async def async_step_reconfigure_au(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_au",
            menu_options=["reconfigure_au_state", "reconfigure"],
        )

    async def async_step_reconfigure_au_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {CONF_PROVIDER: "au", CONF_PROVINCE: user_input[CONF_PROVINCE]}
            return await self._async_update_scoped_entry(entry, new_data)
        return self.async_show_form(
            step_id="reconfigure_au_state",
            data_schema=_state_schema(entry.data.get(CONF_PROVINCE) or None),
        )
