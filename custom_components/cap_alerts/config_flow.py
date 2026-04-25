"""Config flow for CAP Alerts: setup, reconfigure, and options flows."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    CONF_COUNTRY,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    ECCC_PROVINCES,
    METEOALARM_COUNTRIES,
)

# Languages exposed in the options-flow dropdown for MeteoAlarm entries.
# Covers the locales the MeteoAlarm member feeds typically ship plus
# generic English fallback. `auto` resolves to hass.config.language at
# coordinator level.
_METEOALARM_LANGUAGES = (
    "auto",
    "en",
    "de",
    "fr",
    "it",
    "es",
    "nl",
    "pl",
    "pt",
    "cs",
    "sv",
    "no",
    "da",
    "fi",
    "el",
    "hu",
    "ro",
    "bg",
    "hr",
    "sl",
    "sk",
    "et",
    "lv",
    "lt",
    "is",
    "ga",
    "mt",
    "tr",
    "mk",
    "sr",
    "bs",
    "me",
    "he",
)

_GPS_RE = re.compile(r"^-?\d+\.?\d*\s*,\s*-?\d+\.?\d*$")
_ZONE_RE = re.compile(r"^[A-Za-z]{2}[CZ]\d{3}(,[A-Za-z]{2}[CZ]\d{3})*$")


def _compute_device_title(data: dict[str, Any]) -> str:
    """Derive entry title from config data."""
    provider = data[CONF_PROVIDER].upper()
    if CONF_ZONE_ID in data:
        location = data[CONF_ZONE_ID]
    elif CONF_GPS_LOC in data:
        location = data[CONF_GPS_LOC]
    elif CONF_TRACKER_ENTITY in data:
        location = data[CONF_TRACKER_ENTITY].split(".")[-1]
    elif CONF_PROVINCE in data:
        location = data[CONF_PROVINCE]
    elif CONF_COUNTRY in data:
        location = data[CONF_COUNTRY]
    else:
        location = "Unknown"
    return f"CAP Alerts {provider} ({location})"


def _validate_gps(value: str) -> tuple[str, str | None]:
    """Validate GPS string. Returns (cleaned, error_key_or_None)."""
    if not _GPS_RE.match(value):
        return value, "invalid_gps"
    parts = value.split(",")
    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError:
        return value, "invalid_gps"
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return value, "invalid_gps"
    return f"{lat},{lon}", None


def _validate_zone(value: str) -> tuple[str, str | None]:
    """Validate zone ID(s). Returns (cleaned, error_key_or_None)."""
    cleaned = value.strip().upper()
    if not _ZONE_RE.match(cleaned):
        return value, "invalid_zone"
    return cleaned, None


def _validate_province(value: str) -> tuple[str, str | None]:
    """Validate province code. Returns (cleaned, error_key_or_None)."""
    cleaned = value.strip().upper()
    if cleaned not in ECCC_PROVINCES:
        return value, "invalid_province"
    return cleaned, None


def _validate_country(value: str) -> tuple[str, str | None]:
    """Validate MeteoAlarm country code. Returns (cleaned, error_key_or_None)."""
    cleaned = value.strip().upper()
    if not cleaned or cleaned not in METEOALARM_COUNTRIES:
        return value, "invalid_country"
    return cleaned, None


class CAPAlertsFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle config flow for CAP Alerts."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CAPAlertsOptionsFlowHandler()

    # ── Initial setup ──

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Provider selection menu."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["nws", "eccc", "meteoalarm"],
        )

    # ── NWS setup ──

    async def async_step_nws(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """NWS location type menu."""
        return self.async_show_menu(
            step_id="nws",
            menu_options=["nws_zone", "nws_gps_loc", "nws_gps_tracker"],
        )

    async def async_step_nws_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            zone_id, err = _validate_zone(user_input[CONF_ZONE_ID])
            if err:
                errors["base"] = err
            else:
                data = {CONF_PROVIDER: "nws", CONF_ZONE_ID: zone_id}
                return self.async_create_entry(
                    title=_compute_device_title(data), data=data
                )
        return self.async_show_form(
            step_id="nws_zone",
            data_schema=vol.Schema({vol.Required(CONF_ZONE_ID): str}),
            errors=errors,
        )

    async def async_step_nws_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                data = {CONF_PROVIDER: "nws", CONF_GPS_LOC: gps}
                return self.async_create_entry(
                    title=_compute_device_title(data), data=data
                )
        return self.async_show_form(
            step_id="nws_gps_loc",
            data_schema=vol.Schema({vol.Required(CONF_GPS_LOC): str}),
            errors=errors,
        )

    async def async_step_nws_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_PROVIDER: "nws",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_create_entry(title=_compute_device_title(data), data=data)
        return self.async_show_form(
            step_id="nws_gps_tracker",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRACKER_ENTITY): EntitySelector(
                        EntitySelectorConfig(domain="device_tracker")
                    ),
                }
            ),
            errors=errors,
        )

    # ── ECCC setup ──

    async def async_step_eccc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """ECCC location type menu."""
        return self.async_show_menu(
            step_id="eccc",
            menu_options=["eccc_province", "eccc_gps_loc"],
        )

    async def async_step_eccc_province(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            province, err = _validate_province(user_input[CONF_PROVINCE])
            if err:
                errors["base"] = err
            else:
                data = {CONF_PROVIDER: "eccc", CONF_PROVINCE: province}
                return self.async_create_entry(
                    title=_compute_device_title(data), data=data
                )
        return self.async_show_form(
            step_id="eccc_province",
            data_schema=vol.Schema({vol.Required(CONF_PROVINCE): str}),
            errors=errors,
        )

    async def async_step_eccc_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                data = {CONF_PROVIDER: "eccc", CONF_GPS_LOC: gps}
                return self.async_create_entry(
                    title=_compute_device_title(data), data=data
                )
        return self.async_show_form(
            step_id="eccc_gps_loc",
            data_schema=vol.Schema({vol.Required(CONF_GPS_LOC): str}),
            errors=errors,
        )

    # ── MeteoAlarm setup ──

    async def async_step_meteoalarm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """MeteoAlarm: country first, then location filter."""
        return await self.async_step_meteoalarm_country()

    async def async_step_meteoalarm_country(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            country, err = _validate_country(user_input[CONF_COUNTRY])
            if err:
                errors["base"] = err
            else:
                self._meteoalarm_country = country
                return await self.async_step_meteoalarm_filter()
        return self.async_show_form(
            step_id="meteoalarm_country",
            data_schema=vol.Schema({vol.Required(CONF_COUNTRY): str}),
            errors=errors,
        )

    async def async_step_meteoalarm_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="meteoalarm_filter",
            menu_options=["meteoalarm_country_only", "meteoalarm_gps_loc"],
        )

    async def async_step_meteoalarm_country_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        country = getattr(self, "_meteoalarm_country", "")
        data = {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: country}
        return self.async_create_entry(title=_compute_device_title(data), data=data)

    async def async_step_meteoalarm_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        country = getattr(self, "_meteoalarm_country", "")
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                data = {
                    CONF_PROVIDER: "meteoalarm",
                    CONF_COUNTRY: country,
                    CONF_GPS_LOC: gps,
                }
                # Title is GPS-based when GPS is present (matches NWS/ECCC).
                return self.async_create_entry(
                    title=_compute_device_title(data), data=data
                )
        return self.async_show_form(
            step_id="meteoalarm_gps_loc",
            data_schema=vol.Schema({vol.Required(CONF_GPS_LOC): str}),
            errors=errors,
        )

    # ── Reconfigure flow ──

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow full reconfiguration including provider change."""
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                "reconfigure_nws",
                "reconfigure_eccc",
                "reconfigure_meteoalarm",
            ],
        )

    async def async_step_reconfigure_nws(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_nws",
            menu_options=[
                "reconfigure_nws_zone",
                "reconfigure_nws_gps_loc",
                "reconfigure_nws_gps_tracker",
            ],
        )

    async def async_step_reconfigure_nws_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            zone_id, err = _validate_zone(user_input[CONF_ZONE_ID])
            if err:
                errors["base"] = err
            else:
                new_data = {CONF_PROVIDER: "nws", CONF_ZONE_ID: zone_id}
                return self.async_update_reload_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )
        return self.async_show_form(
            step_id="reconfigure_nws_zone",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ZONE_ID, default=entry.data.get(CONF_ZONE_ID, "")
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_nws_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                new_data = {CONF_PROVIDER: "nws", CONF_GPS_LOC: gps}
                return self.async_update_reload_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )
        return self.async_show_form(
            step_id="reconfigure_nws_gps_loc",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GPS_LOC, default=entry.data.get(CONF_GPS_LOC, "")
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_nws_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {
                CONF_PROVIDER: "nws",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_update_reload_and_abort(
                entry, data=new_data, title=_compute_device_title(new_data)
            )
        return self.async_show_form(
            step_id="reconfigure_nws_gps_tracker",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRACKER_ENTITY,
                        default=entry.data.get(CONF_TRACKER_ENTITY, ""),
                    ): EntitySelector(EntitySelectorConfig(domain="device_tracker")),
                }
            ),
        )

    async def async_step_reconfigure_eccc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_eccc",
            menu_options=["reconfigure_eccc_province", "reconfigure_eccc_gps_loc"],
        )

    async def async_step_reconfigure_eccc_province(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            province, err = _validate_province(user_input[CONF_PROVINCE])
            if err:
                errors["base"] = err
            else:
                new_data = {CONF_PROVIDER: "eccc", CONF_PROVINCE: province}
                return self.async_update_reload_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )
        return self.async_show_form(
            step_id="reconfigure_eccc_province",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROVINCE, default=entry.data.get(CONF_PROVINCE, "")
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_eccc_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                new_data = {CONF_PROVIDER: "eccc", CONF_GPS_LOC: gps}
                return self.async_update_reload_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )
        return self.async_show_form(
            step_id="reconfigure_eccc_gps_loc",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GPS_LOC, default=entry.data.get(CONF_GPS_LOC, "")
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_meteoalarm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_reconfigure_meteoalarm_country()

    async def async_step_reconfigure_meteoalarm_country(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            country, err = _validate_country(user_input[CONF_COUNTRY])
            if err:
                errors["base"] = err
            else:
                self._meteoalarm_country = country
                return await self.async_step_reconfigure_meteoalarm_filter()
        return self.async_show_form(
            step_id="reconfigure_meteoalarm_country",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COUNTRY, default=entry.data.get(CONF_COUNTRY, "")
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_meteoalarm_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_meteoalarm_filter",
            menu_options=[
                "reconfigure_meteoalarm_country_only",
                "reconfigure_meteoalarm_gps_loc",
            ],
        )

    async def async_step_reconfigure_meteoalarm_country_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        country = getattr(self, "_meteoalarm_country", "")
        new_data = {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: country}
        return self.async_update_reload_and_abort(
            entry, data=new_data, title=_compute_device_title(new_data)
        )

    async def async_step_reconfigure_meteoalarm_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        country = getattr(self, "_meteoalarm_country", "")
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                new_data = {
                    CONF_PROVIDER: "meteoalarm",
                    CONF_COUNTRY: country,
                    CONF_GPS_LOC: gps,
                }
                return self.async_update_reload_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )
        return self.async_show_form(
            step_id="reconfigure_meteoalarm_gps_loc",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GPS_LOC, default=entry.data.get(CONF_GPS_LOC, "")
                    ): str,
                }
            ),
            errors=errors,
        )


class CAPAlertsOptionsFlowHandler(OptionsFlow):
    """Handle options flow for CAP Alerts."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        provider = self.config_entry.data.get(CONF_PROVIDER)
        schema: dict[vol.Optional, Any] = {
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=self.config_entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
            vol.Optional(
                CONF_TIMEOUT,
                default=self.config_entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
        }

        if provider == "eccc":
            schema[
                vol.Optional(
                    CONF_LANGUAGE,
                    default=self.config_entry.options.get(CONF_LANGUAGE, "auto"),
                )
            ] = vol.In(["auto", "en-CA", "fr-CA"])
        elif provider == "meteoalarm":
            schema[
                vol.Optional(
                    CONF_LANGUAGE,
                    default=self.config_entry.options.get(CONF_LANGUAGE, "auto"),
                )
            ] = vol.In(list(_METEOALARM_LANGUAGES))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
        )
