"""BBK / NINA setup, reconfigure, and options steps (issue #66)."""

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
    BBK_LANGUAGES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
)
from ..providers.bbk import normalize_ars
from .common import (
    OptionsSchema,
    ScopedEntryFlowMixin,
    _gps_schema,
    _home_gps,
    _tracker_schema,
    _validate_gps,
)


def _validate_ars(value: str) -> tuple[str, str | None]:
    """Validate a typed Regionalschlüssel. Returns (cleaned, error_key_or_None).

    Syntax only: five to twelve digits, widened to the district the dashboard
    answers at (``providers.bbk.normalize_ars``). Whether a district exists
    under the code is the provider's scope check, which asks the dashboard.
    """
    ars = normalize_ars(value)
    if ars is None:
        return value, "invalid_bbk_region"
    return ars, None


def _region_schema(default: str | None = None) -> vol.Schema:
    """Schema with the single Regionalschlüssel text field.

    Plain text rather than a picker: the official municipality registry runs
    to 11,284 rows and 430 KB, six seconds to fetch on 2026-09-19, and the
    dashboard only answers at district level anyway. A user finds their code
    once (it is printed on the NINA app's region page and on every
    Regionalschlüssel lookup site) and the form validates it live.
    """
    if default is not None:
        key: Any = vol.Required(CONF_ZONE_ID, default=default)
    else:
        key = vol.Required(CONF_ZONE_ID)
    return vol.Schema({key: str})


def _bbk_language_selector() -> SelectSelector:
    """Dropdown of the ``info[]`` languages BBK publishes.

    No custom value: unlike WMO's 140 sources, the set is one feed's and it is
    fixed by the API (``BBK_LANGUAGES``). ``sort=False`` keeps ``auto`` first
    and the two German registers together.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=code, label=code) for code in BBK_LANGUAGES
            ],
            mode=SelectSelectorMode.DROPDOWN,
            sort=False,
        )
    )


def options_schema(entry: ConfigEntry) -> OptionsSchema:
    """BBK-specific option fields: which ``info[]`` language to read."""
    return {
        vol.Optional(
            CONF_LANGUAGE,
            default=entry.options.get(CONF_LANGUAGE, "auto"),
        ): _bbk_language_selector(),
    }


class BBKFlowMixin(ScopedEntryFlowMixin):
    """BBK steps, mixed into the domain's flow handler."""

    # ── BBK setup ──

    async def async_step_bbk(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """BBK location scope menu.

        Scope only. There is deliberately no channel or severity option (see
        the issue #66 discussion): the civil-protection channels are the
        reason the provider exists and the DWD relay comes with them.
        """
        return self.async_show_menu(
            step_id="bbk",
            # "user" is the back edge (issue #140); see the NWS menu.
            menu_options=["bbk_region", "bbk_gps_loc", "bbk_gps_tracker", "user"],
        )

    async def async_step_bbk_region(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            ars, err = _validate_ars(user_input[CONF_ZONE_ID])
            if err:
                errors["base"] = err
            else:
                data = {CONF_PROVIDER: "bbk", CONF_ZONE_ID: ars}
                if err := await self._async_validate_scope(data):
                    errors["base"] = err
                else:
                    return await self._async_create_scoped_entry(data)
        return self.async_show_form(
            step_id="bbk_region",
            data_schema=_region_schema(),
            errors=errors,
        )

    async def async_step_bbk_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                data = {CONF_PROVIDER: "bbk", CONF_GPS_LOC: gps}
                return await self._async_create_scoped_entry(data)
        return self.async_show_form(
            step_id="bbk_gps_loc",
            data_schema=_gps_schema(_home_gps(self.hass)),
            errors=errors,
        )

    async def async_step_bbk_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = {
                CONF_PROVIDER: "bbk",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return await self._async_create_scoped_entry(data)
        return self.async_show_form(
            step_id="bbk_gps_tracker",
            data_schema=_tracker_schema(),
        )

    # ── BBK reconfigure ──

    async def async_step_reconfigure_bbk(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_bbk",
            menu_options=[
                "reconfigure_bbk_region",
                "reconfigure_bbk_gps_loc",
                "reconfigure_bbk_gps_tracker",
                "reconfigure",
            ],
        )

    async def async_step_reconfigure_bbk_region(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            ars, err = _validate_ars(user_input[CONF_ZONE_ID])
            if err:
                errors["base"] = err
            else:
                new_data = {CONF_PROVIDER: "bbk", CONF_ZONE_ID: ars}
                if err := await self._async_validate_scope(new_data):
                    errors["base"] = err
                else:
                    return await self._async_update_scoped_entry(entry, new_data)
        return self.async_show_form(
            step_id="reconfigure_bbk_region",
            data_schema=_region_schema(entry.data.get(CONF_ZONE_ID) or None),
            errors=errors,
        )

    async def async_step_reconfigure_bbk_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                new_data = {CONF_PROVIDER: "bbk", CONF_GPS_LOC: gps}
                return await self._async_update_scoped_entry(entry, new_data)
        return self.async_show_form(
            step_id="reconfigure_bbk_gps_loc",
            data_schema=_gps_schema(
                entry.data.get(CONF_GPS_LOC) or _home_gps(self.hass)
            ),
            errors=errors,
        )

    async def async_step_reconfigure_bbk_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {
                CONF_PROVIDER: "bbk",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return await self._async_update_scoped_entry(entry, new_data)
        return self.async_show_form(
            step_id="reconfigure_bbk_gps_tracker",
            data_schema=_tracker_schema(
                default=entry.data.get(CONF_TRACKER_ENTITY, "")
            ),
        )
