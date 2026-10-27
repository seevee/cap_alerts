"""GeoSphere Austria setup, reconfigure, and options steps (issue #300)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import (
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_TRACKER_ENTITY,
    GEOSPHERE_LANGUAGES,
)
from ..schema import vol
from .common import (
    OptionsSchema,
    ScopedEntryFlowMixin,
    _gps_schema,
    _home_gps,
    _tracker_schema,
    _validate_gps,
)


def _geosphere_language_selector() -> SelectSelector:
    """Dropdown of the two ``lang`` values the API takes, German first."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=code, label=code) for code in GEOSPHERE_LANGUAGES
            ],
            mode=SelectSelectorMode.DROPDOWN,
            sort=False,
        )
    )


def options_schema(entry: ConfigEntry) -> OptionsSchema:
    """GeoSphere-specific option fields: which language to ask the API for."""
    return {
        vol.Optional(
            CONF_LANGUAGE,
            default=entry.options.get(CONF_LANGUAGE, GEOSPHERE_LANGUAGES[0]),
        ): _geosphere_language_selector(),
    }


class GeoSphereFlowMixin(ScopedEntryFlowMixin):
    """GeoSphere steps, mixed into the domain's flow handler."""

    # ── GeoSphere setup ──

    async def async_step_geosphere(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """GeoSphere location scope menu.

        No municipality picker: there are over 2,000, and the API resolves a
        point to its municipality on every request anyway.
        """
        return self.async_show_menu(
            step_id="geosphere",
            # "user" is the back edge (issue #140); see the NWS menu.
            menu_options=["geosphere_gps_loc", "geosphere_gps_tracker", "user"],
        )

    async def async_step_geosphere_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                data = {CONF_PROVIDER: "geosphere", CONF_GPS_LOC: gps}
                if err := await self._async_validate_scope(data):
                    errors["base"] = err
                else:
                    return await self._async_create_scoped_entry(data)
        return self.async_show_form(
            step_id="geosphere_gps_loc",
            data_schema=_gps_schema(_home_gps(self.hass)),
            errors=errors,
        )

    async def async_step_geosphere_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = {
                CONF_PROVIDER: "geosphere",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return await self._async_create_scoped_entry(data)
        return self.async_show_form(
            step_id="geosphere_gps_tracker",
            data_schema=_tracker_schema(),
        )

    # ── GeoSphere reconfigure ──

    async def async_step_reconfigure_geosphere(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_geosphere",
            menu_options=[
                "reconfigure_geosphere_gps_loc",
                "reconfigure_geosphere_gps_tracker",
                "reconfigure",
            ],
        )

    async def async_step_reconfigure_geosphere_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                new_data = {CONF_PROVIDER: "geosphere", CONF_GPS_LOC: gps}
                if err := await self._async_validate_scope(new_data):
                    errors["base"] = err
                else:
                    return await self._async_update_scoped_entry(entry, new_data)
        return self.async_show_form(
            step_id="reconfigure_geosphere_gps_loc",
            data_schema=_gps_schema(
                entry.data.get(CONF_GPS_LOC) or _home_gps(self.hass)
            ),
            errors=errors,
        )

    async def async_step_reconfigure_geosphere_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {
                CONF_PROVIDER: "geosphere",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return await self._async_update_scoped_entry(entry, new_data)
        return self.async_show_form(
            step_id="reconfigure_geosphere_gps_tracker",
            data_schema=_tracker_schema(
                default=entry.data.get(CONF_TRACKER_ENTITY, "")
            ),
        )
