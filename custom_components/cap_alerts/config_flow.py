"""Config flow for CAP Alerts: setup, reconfigure, and options flows."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ATTRIBUTE,
    CONF_COUNTRY_ENTITY,
    CONF_EXCLUDE_MARINE,
    CONF_FEED_SOURCE,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_PROVINCE,
    CONF_REGION_LABELS,
    CONF_REGIONS,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE_ID,
    CONF_STREAMING,
    CONF_TIMEOUT,
    CONF_TRACKER_ENTITY,
    CONF_ZONE_ID,
    DEFAULT_FEED_SOURCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    ECCC_PROVINCES,
    METEOALARM_COUNTRIES,
    METEOALARM_COUNTRY_NAMES,
    WMO_LANGUAGES,
    WMO_SOURCE_NAMES,
)
from .providers.meteoalarm import fetch_regions_for_country
from .providers.wmo import fetch_wmo_sources

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
# WMO SWIC source IDs: {country}-{agency}[-{extra}…] (e.g. mx-smn-es,
# us-noaa-nws-en-marine). The trailing segment is NOT a reliable language —
# 17 registry IDs end in "-xx", one ends in "-marine", and 15 of the 110
# sources sampled disagree with their CAP body's first <info> block. Body
# language is selected at fetch time (providers/wmo.py:_select_info).
_WMO_SOURCE_RE = re.compile(r"^[a-z]{2}(-[a-z0-9]+){2,4}$")
# One area-code prefix. Deliberately not numeric-only: the filter compares
# against every geocode scheme a feed publishes, which includes alphabetic ones
# (NWS ``UGC`` "OHZ049", MeteoAlarm ``EMMA_ID`` "DE123"), so a digits-only rule
# would make the feature China-specific.
_GEOCODE_PREFIX_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,32}$")


def _tracker_schema(default: str | None = None) -> vol.Schema:
    """Schema with a single ``device_tracker`` entity selector.

    Shared by every provider's GPS-tracker step. ``default`` carries the
    current entity id forward in reconfigure flows.
    """
    if default is not None:
        key: Any = vol.Required(CONF_TRACKER_ENTITY, default=default)
    else:
        key = vol.Required(CONF_TRACKER_ENTITY)
    return vol.Schema(
        {key: EntitySelector(EntitySelectorConfig(domain="device_tracker"))}
    )


def _country_source_schema(
    tracker_default: str | None = None,
    country_default: str | None = None,
    attribute_default: str | None = None,
) -> vol.Schema:
    """Schema for MeteoAlarm fully-mobile mode.

    A ``device_tracker`` for coordinates, an (unrestricted) entity whose value
    names the country, and an optional attribute to read that country from.
    Defaults carry current values forward in reconfigure flows.
    """
    if tracker_default is not None:
        tracker_key: Any = vol.Required(CONF_TRACKER_ENTITY, default=tracker_default)
    else:
        tracker_key = vol.Required(CONF_TRACKER_ENTITY)
    if country_default is not None:
        country_key: Any = vol.Required(CONF_COUNTRY_ENTITY, default=country_default)
    else:
        country_key = vol.Required(CONF_COUNTRY_ENTITY)
    attribute_key: Any = vol.Optional(
        CONF_COUNTRY_ATTRIBUTE, default=attribute_default or ""
    )
    return vol.Schema(
        {
            tracker_key: EntitySelector(EntitySelectorConfig(domain="device_tracker")),
            country_key: EntitySelector(EntitySelectorConfig()),
            attribute_key: str,
        }
    )


def _compute_device_title(data: dict[str, Any]) -> str:
    """Derive entry title from config data."""
    provider = data[CONF_PROVIDER].upper()
    if data[CONF_PROVIDER] == "wmo":
        source_id = data.get(CONF_SOURCE_ID, "unknown")
        source_name = WMO_SOURCE_NAMES.get(source_id, source_id)
        if CONF_GPS_LOC in data:
            location = f"{source_name} ({data[CONF_GPS_LOC]})"
        elif CONF_TRACKER_ENTITY in data:
            location = f"{source_name} ({data[CONF_TRACKER_ENTITY].split('.')[-1]})"
        else:
            location = source_name
    elif CONF_COUNTRY_ENTITY in data:
        # MeteoAlarm fully-mobile mode: country follows a source entity, so
        # there is no static location — surface the tracker name as "auto".
        location = f"auto: {data[CONF_TRACKER_ENTITY].split('.')[-1]}"
    elif CONF_ZONE_ID in data:
        location = data[CONF_ZONE_ID]
    elif CONF_GPS_LOC in data:
        location = data[CONF_GPS_LOC]
    elif CONF_TRACKER_ENTITY in data:
        location = data[CONF_TRACKER_ENTITY].split(".")[-1]
    elif CONF_PROVINCE in data:
        location = data[CONF_PROVINCE]
    elif CONF_REGIONS in data:
        country_code = data.get(CONF_COUNTRY, "")
        country_name = METEOALARM_COUNTRY_NAMES.get(country_code, country_code)
        labels = data.get(CONF_REGION_LABELS) or {}
        if labels:
            sorted_labels = sorted(labels.values())
            # Counted from the authoritative selection, not the label map:
            # a legacy entry may carry fewer labels than selected regions.
            extra = len(data[CONF_REGIONS]) - 1
            suffix = f" +{extra}" if extra > 0 else ""
            location = f"{country_code} — {sorted_labels[0]}{suffix}"
        else:
            count = len(data[CONF_REGIONS])
            location = f"{country_name} — {count} regions"
    elif CONF_COUNTRY in data:
        code = data[CONF_COUNTRY]
        location = METEOALARM_COUNTRY_NAMES.get(code, code)
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


def _validate_wmo_source(value: str) -> tuple[str, str | None]:
    """Validate a WMO source ID. Returns (cleaned, error_key_or_None).

    Accepts both catalog entries and free-text that matches the
    ``{country}-{agency}[-{extra}…]`` source-ID shape, so advanced users can
    enter sources not yet in ``WMO_SOURCE_NAMES``.
    """
    cleaned = value.strip().lower()
    if not cleaned:
        return value, "invalid_wmo_source"
    if cleaned not in WMO_SOURCE_NAMES and not _WMO_SOURCE_RE.match(cleaned):
        return value, "invalid_wmo_source"
    return cleaned, None


def _validate_geocode_prefixes(value: str) -> tuple[list[str], str | None]:
    """Parse a comma-separated area-code prefix list.

    Returns ``(prefixes, error_key_or_None)``. Empty input is *valid* and
    yields ``[]`` — this is an optional narrowing, and clearing the field is
    how a user turns it back off. Tokens are stripped, empties dropped, and
    duplicates collapsed order-preservingly. Stored verbatim rather than
    upper-cased so the user's input stays recognisable when the form is
    re-rendered; the filter casefolds at comparison time.
    """
    prefixes: list[str] = []
    for token in value.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        if not _GEOCODE_PREFIX_RE.match(cleaned):
            return [], "invalid_geocode_prefix"
        if cleaned not in prefixes:
            prefixes.append(cleaned)
    return prefixes, None


def _wmo_source_selector(options: list[tuple[str, str]]) -> SelectSelector:
    """Dropdown selector for WMO source IDs, allowing custom IDs.

    ``options`` is ``[(sourceId, label), ...]``, built dynamically from the
    live SWIC registry (or the static ``WMO_SOURCE_NAMES`` fallback).
    """
    select_options = [
        SelectOptionDict(value=sid, label=label) for sid, label in options
    ]
    return SelectSelector(
        SelectSelectorConfig(
            options=select_options,
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=True,
            sort=True,
        )
    )


def _wmo_language_selector() -> SelectSelector:
    """Dropdown of WMO body languages, allowing any custom BCP 47 tag.

    ``WMO_LANGUAGES`` seeds the list with the primary subtags actually seen in
    SWIC bodies, but a source may publish a script- or region-tagged form
    (``zh-Hans``, ``pt-BR``, ``sr-Latn``), so free text is accepted too — the
    provider's matcher falls back to English then document order for anything
    a document does not carry. ``sort=False`` keeps ``auto`` first.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=code, label=code) for code in WMO_LANGUAGES
            ],
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=True,
            sort=False,
        )
    )


def _country_selector() -> SelectSelector:
    """Dropdown selector backed by ``METEOALARM_COUNTRY_NAMES``."""
    options = [
        SelectOptionDict(value=iso, label=METEOALARM_COUNTRY_NAMES[iso])
        for iso in METEOALARM_COUNTRY_NAMES
    ]
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            mode=SelectSelectorMode.DROPDOWN,
            sort=True,
        )
    )


def _region_selector(
    regions: list[tuple[str, str]],
    *,
    extra: Sequence[tuple[str, str]] = (),
) -> SelectSelector:
    """Multi-select dropdown for region codes, accepting typed-in codes.

    The list can only offer regions named by warnings currently in the feed
    (the regions endpoint is 404 for every country), so ``custom_value``
    lets a user enter a code no live warning mentions — e.g. an ``EMMA_ID``
    whose warning has expired. ``extra`` carries already-configured codes that
    the current fetch does not offer, so reconfigure renders them as real
    options instead of dropping them.
    """
    options = [
        SelectOptionDict(value=code, label=label) for code, label in [*regions, *extra]
    ]
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            mode=SelectSelectorMode.DROPDOWN,
            multiple=True,
            custom_value=True,
            sort=True,
        )
    )


def _normalize_region_selection(selected: list[Any]) -> list[str]:
    """Clean a region multi-select: stringify, strip, drop empties, de-dup.

    Typed-in values arrive as free text, so they can carry stray whitespace or
    be entered twice; codes span several namespaces (``EMMA_ID``, ``NUTS3``,
    ``NUTS2``, and the ``areaDesc`` fallback) and are deliberately not
    validated beyond this — any pattern tight enough to catch a typo would
    reject a legitimate namespace, and a wrong code simply matches nothing.
    """
    out: list[str] = []
    for value in selected:
        code = str(value).strip()
        if code and code not in out:
            out.append(code)
    return out


def _region_label_map(
    selected: list[str],
    fetched: dict[str, str],
    stored: dict[str, str],
) -> dict[str, str]:
    """Label every selected code: fetched label, else stored, else the code."""
    return {code: fetched.get(code) or stored.get(code) or code for code in selected}


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
            menu_options=["nws", "eccc", "meteoalarm", "wmo"],
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
            data_schema=_tracker_schema(),
            errors=errors,
        )

    # ── ECCC setup ──

    async def async_step_eccc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """ECCC location type menu."""
        return self.async_show_menu(
            step_id="eccc",
            menu_options=["eccc_province", "eccc_gps_loc", "eccc_gps_tracker"],
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

    async def async_step_eccc_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = {
                CONF_PROVIDER: "eccc",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_create_entry(title=_compute_device_title(data), data=data)
        return self.async_show_form(
            step_id="eccc_gps_tracker",
            data_schema=_tracker_schema(),
        )

    # ── MeteoAlarm setup ──

    async def async_step_meteoalarm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """MeteoAlarm: pick a fixed country or the fully-mobile mode."""
        return self.async_show_menu(
            step_id="meteoalarm",
            menu_options=["meteoalarm_country", "meteoalarm_country_source"],
        )

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
            data_schema=vol.Schema({vol.Required(CONF_COUNTRY): _country_selector()}),
            errors=errors,
        )

    async def async_step_meteoalarm_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="meteoalarm_filter",
            menu_options=[
                "meteoalarm_country_only",
                "meteoalarm_gps_polygon",
                "meteoalarm_gps_tracker",
                "meteoalarm_region_picker",
            ],
        )

    async def async_step_meteoalarm_country_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        country = getattr(self, "_meteoalarm_country", "")
        data = {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: country}
        return self.async_create_entry(title=_compute_device_title(data), data=data)

    async def async_step_meteoalarm_gps_polygon(
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
            step_id="meteoalarm_gps_polygon",
            data_schema=vol.Schema({vol.Required(CONF_GPS_LOC): str}),
            errors=errors,
        )

    async def async_step_meteoalarm_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        country = getattr(self, "_meteoalarm_country", "")
        if user_input is not None:
            data = {
                CONF_PROVIDER: "meteoalarm",
                CONF_COUNTRY: country,
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_create_entry(title=_compute_device_title(data), data=data)
        return self.async_show_form(
            step_id="meteoalarm_gps_tracker",
            data_schema=_tracker_schema(),
        )

    async def async_step_meteoalarm_country_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fully-mobile MeteoAlarm: tracker for coords + country-source entity."""
        if user_input is not None:
            data = {
                CONF_PROVIDER: "meteoalarm",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
                CONF_COUNTRY_ENTITY: user_input[CONF_COUNTRY_ENTITY],
            }
            attribute = (user_input.get(CONF_COUNTRY_ATTRIBUTE) or "").strip()
            if attribute:
                data[CONF_COUNTRY_ATTRIBUTE] = attribute
            return self.async_create_entry(title=_compute_device_title(data), data=data)
        return self.async_show_form(
            step_id="meteoalarm_country_source",
            data_schema=_country_source_schema(),
        )

    async def async_step_meteoalarm_region_picker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        country = getattr(self, "_meteoalarm_country", "")
        errors: dict[str, str] = {}

        try:
            regions = await fetch_regions_for_country(
                async_get_clientsession(self.hass), country
            )
        except UpdateFailed:
            return self.async_show_form(
                step_id="meteoalarm_region_picker",
                data_schema=vol.Schema({}),
                errors={"base": "cannot_fetch_regions"},
            )

        if user_input is not None:
            selected = _normalize_region_selection(user_input.get(CONF_REGIONS) or [])
            if not selected:
                errors["base"] = "no_regions_selected"
            else:
                data = {
                    CONF_PROVIDER: "meteoalarm",
                    CONF_COUNTRY: country,
                    CONF_REGIONS: selected,
                    CONF_REGION_LABELS: _region_label_map(selected, dict(regions), {}),
                }
                return self.async_create_entry(
                    title=_compute_device_title(data), data=data
                )

        return self.async_show_form(
            step_id="meteoalarm_region_picker",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REGIONS, default=[]): _region_selector(regions),
                }
            ),
            errors=errors,
        )

    # ── WMO setup ──

    async def _wmo_source_options(self) -> list[tuple[str, str]]:
        """Build the WMO source dropdown options.

        Fetches the live SWIC registry once per flow (cached on the handler so
        a re-rendered form doesn't re-fetch); on any failure falls back to the
        static ``WMO_SOURCE_NAMES`` catalog so setup never hard-fails.
        """
        cached = getattr(self, "_wmo_source_options_cache", None)
        if cached is not None:
            return cached
        options = await fetch_wmo_sources(async_get_clientsession(self.hass))
        if not options:
            options = sorted(WMO_SOURCE_NAMES.items(), key=lambda item: item[1])
        self._wmo_source_options_cache = options
        return options

    async def async_step_wmo(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """WMO: source first, then location filter."""
        return await self.async_step_wmo_source()

    async def async_step_wmo_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            source_id, err = _validate_wmo_source(user_input[CONF_SOURCE_ID])
            if err:
                errors["base"] = err
            else:
                self._wmo_source_id = source_id
                return await self.async_step_wmo_filter()
        options = await self._wmo_source_options()
        return self.async_show_form(
            step_id="wmo_source",
            data_schema=vol.Schema(
                {vol.Required(CONF_SOURCE_ID): _wmo_source_selector(options)}
            ),
            errors=errors,
        )

    async def async_step_wmo_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="wmo_filter",
            menu_options=[
                "wmo_country_wide",
                "wmo_gps_loc",
                "wmo_gps_tracker",
                "wmo_geocode",
            ],
        )

    async def async_step_wmo_country_wide(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        source_id = getattr(self, "_wmo_source_id", "")
        data = {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: source_id}
        return self.async_create_entry(title=_compute_device_title(data), data=data)

    async def async_step_wmo_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        source_id = getattr(self, "_wmo_source_id", "")
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                data = {
                    CONF_PROVIDER: "wmo",
                    CONF_SOURCE_ID: source_id,
                    CONF_GPS_LOC: gps,
                }
                return self.async_create_entry(
                    title=_compute_device_title(data), data=data
                )
        return self.async_show_form(
            step_id="wmo_gps_loc",
            data_schema=vol.Schema({vol.Required(CONF_GPS_LOC): str}),
            errors=errors,
        )

    async def async_step_wmo_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        source_id = getattr(self, "_wmo_source_id", "")
        if user_input is not None:
            data = {
                CONF_PROVIDER: "wmo",
                CONF_SOURCE_ID: source_id,
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_create_entry(title=_compute_device_title(data), data=data)
        return self.async_show_form(
            step_id="wmo_gps_tracker",
            data_schema=_tracker_schema(),
        )

    async def async_step_wmo_geocode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Country-wide fetch narrowed by area-code prefixes.

        The prefixes are an *option*, not entry data — the filter is
        provider-neutral and editable later in the options flow. This step only
        exists so a high-volume source can be narrowed **before** the first
        refresh: setting it afterwards means creating hundreds of entities and
        immediately removing most of them (``cn-cma-xx`` publishes ~260 active
        alerts country-wide, ~30 for prefix ``13``).
        """
        errors: dict[str, str] = {}
        source_id = getattr(self, "_wmo_source_id", "")
        if user_input is not None:
            prefixes, err = _validate_geocode_prefixes(
                str(user_input.get(CONF_GEOCODE_PREFIXES, "") or "")
            )
            # Unlike the options-flow field, empty is rejected here: the user
            # picked this mode, so an empty value is a mistake rather than
            # "filter off".
            if err or not prefixes:
                errors["base"] = err or "invalid_geocode_prefix"
            else:
                data = {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: source_id}
                return self.async_create_entry(
                    title=_compute_device_title(data),
                    data=data,
                    options={CONF_GEOCODE_PREFIXES: prefixes},
                )
        return self.async_show_form(
            step_id="wmo_geocode",
            data_schema=vol.Schema({vol.Required(CONF_GEOCODE_PREFIXES): str}),
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
                "reconfigure_wmo",
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
                return self.async_update_and_abort(
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
                return self.async_update_and_abort(
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
            return self.async_update_and_abort(
                entry, data=new_data, title=_compute_device_title(new_data)
            )
        return self.async_show_form(
            step_id="reconfigure_nws_gps_tracker",
            data_schema=_tracker_schema(
                default=entry.data.get(CONF_TRACKER_ENTITY, "")
            ),
        )

    async def async_step_reconfigure_eccc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_eccc",
            menu_options=[
                "reconfigure_eccc_province",
                "reconfigure_eccc_gps_loc",
                "reconfigure_eccc_gps_tracker",
            ],
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
                return self.async_update_and_abort(
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
                return self.async_update_and_abort(
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

    async def async_step_reconfigure_eccc_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {
                CONF_PROVIDER: "eccc",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_update_and_abort(
                entry, data=new_data, title=_compute_device_title(new_data)
            )
        return self.async_show_form(
            step_id="reconfigure_eccc_gps_tracker",
            data_schema=_tracker_schema(
                default=entry.data.get(CONF_TRACKER_ENTITY, "")
            ),
        )

    async def async_step_reconfigure_meteoalarm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_meteoalarm",
            menu_options=[
                "reconfigure_meteoalarm_country",
                "reconfigure_meteoalarm_country_source",
            ],
        )

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
        existing = entry.data.get(CONF_COUNTRY, "")
        country_kwargs: dict[str, Any] = {}
        if existing in METEOALARM_COUNTRIES:
            country_kwargs["default"] = existing
        return self.async_show_form(
            step_id="reconfigure_meteoalarm_country",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COUNTRY, **country_kwargs): _country_selector(),
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
                "reconfigure_meteoalarm_gps_polygon",
                "reconfigure_meteoalarm_gps_tracker",
                "reconfigure_meteoalarm_region_picker",
            ],
        )

    async def async_step_reconfigure_meteoalarm_country_only(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        country = getattr(self, "_meteoalarm_country", "")
        new_data = {CONF_PROVIDER: "meteoalarm", CONF_COUNTRY: country}
        return self.async_update_and_abort(
            entry, data=new_data, title=_compute_device_title(new_data)
        )

    async def async_step_reconfigure_meteoalarm_gps_polygon(
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
                return self.async_update_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )
        return self.async_show_form(
            step_id="reconfigure_meteoalarm_gps_polygon",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GPS_LOC, default=entry.data.get(CONF_GPS_LOC, "")
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_meteoalarm_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        country = getattr(self, "_meteoalarm_country", "")
        if user_input is not None:
            new_data = {
                CONF_PROVIDER: "meteoalarm",
                CONF_COUNTRY: country,
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_update_and_abort(
                entry, data=new_data, title=_compute_device_title(new_data)
            )
        return self.async_show_form(
            step_id="reconfigure_meteoalarm_gps_tracker",
            data_schema=_tracker_schema(
                default=entry.data.get(CONF_TRACKER_ENTITY, "")
            ),
        )

    async def async_step_reconfigure_meteoalarm_country_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {
                CONF_PROVIDER: "meteoalarm",
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
                CONF_COUNTRY_ENTITY: user_input[CONF_COUNTRY_ENTITY],
            }
            attribute = (user_input.get(CONF_COUNTRY_ATTRIBUTE) or "").strip()
            if attribute:
                new_data[CONF_COUNTRY_ATTRIBUTE] = attribute
            return self.async_update_and_abort(
                entry, data=new_data, title=_compute_device_title(new_data)
            )
        return self.async_show_form(
            step_id="reconfigure_meteoalarm_country_source",
            data_schema=_country_source_schema(
                tracker_default=entry.data.get(CONF_TRACKER_ENTITY, ""),
                country_default=entry.data.get(CONF_COUNTRY_ENTITY, ""),
                attribute_default=entry.data.get(CONF_COUNTRY_ATTRIBUTE, ""),
            ),
        )

    async def async_step_reconfigure_meteoalarm_region_picker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        country = getattr(self, "_meteoalarm_country", "")
        errors: dict[str, str] = {}

        try:
            regions = await fetch_regions_for_country(
                async_get_clientsession(self.hass), country
            )
        except UpdateFailed:
            return self.async_show_form(
                step_id="reconfigure_meteoalarm_region_picker",
                data_schema=vol.Schema({}),
                errors={"base": "cannot_fetch_regions"},
            )

        fetched = dict(regions)
        stored = dict(entry.data.get(CONF_REGION_LABELS) or {})

        if user_input is not None:
            selected = _normalize_region_selection(user_input.get(CONF_REGIONS) or [])
            if not selected:
                errors["base"] = "no_regions_selected"
            else:
                new_data = {
                    CONF_PROVIDER: "meteoalarm",
                    CONF_COUNTRY: country,
                    CONF_REGIONS: selected,
                    CONF_REGION_LABELS: _region_label_map(selected, fetched, stored),
                }
                return self.async_update_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )

        # Carry every stored selection forward, including codes the current
        # fetch doesn't offer (typed-in, or their warning has since expired):
        # they are injected as options so the form can render them as selected.
        existing = _normalize_region_selection(entry.data.get(CONF_REGIONS) or [])
        extra = [
            (code, stored.get(code) or code) for code in existing if code not in fetched
        ]
        return self.async_show_form(
            step_id="reconfigure_meteoalarm_region_picker",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REGIONS, default=existing): _region_selector(
                        regions, extra=extra
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_wmo(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_reconfigure_wmo_source()

    async def async_step_reconfigure_wmo_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            source_id, err = _validate_wmo_source(user_input[CONF_SOURCE_ID])
            if err:
                errors["base"] = err
            else:
                self._wmo_source_id = source_id
                return await self.async_step_reconfigure_wmo_filter()
        existing = entry.data.get(CONF_SOURCE_ID, "")
        source_kwargs: dict[str, Any] = {}
        if existing:
            source_kwargs["default"] = existing
        options = await self._wmo_source_options()
        return self.async_show_form(
            step_id="reconfigure_wmo_source",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCE_ID, **source_kwargs): _wmo_source_selector(
                        options
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_wmo_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_wmo_filter",
            menu_options=[
                "reconfigure_wmo_country_wide",
                "reconfigure_wmo_gps_loc",
                "reconfigure_wmo_gps_tracker",
                "reconfigure_wmo_geocode",
            ],
        )

    async def async_step_reconfigure_wmo_country_wide(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        source_id = getattr(self, "_wmo_source_id", "")
        new_data = {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: source_id}
        return self.async_update_and_abort(
            entry, data=new_data, title=_compute_device_title(new_data)
        )

    async def async_step_reconfigure_wmo_gps_loc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        source_id = getattr(self, "_wmo_source_id", "")
        if user_input is not None:
            gps, err = _validate_gps(user_input[CONF_GPS_LOC])
            if err:
                errors["base"] = err
            else:
                new_data = {
                    CONF_PROVIDER: "wmo",
                    CONF_SOURCE_ID: source_id,
                    CONF_GPS_LOC: gps,
                }
                return self.async_update_and_abort(
                    entry, data=new_data, title=_compute_device_title(new_data)
                )
        return self.async_show_form(
            step_id="reconfigure_wmo_gps_loc",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GPS_LOC, default=entry.data.get(CONF_GPS_LOC, "")
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_wmo_gps_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        source_id = getattr(self, "_wmo_source_id", "")
        if user_input is not None:
            new_data = {
                CONF_PROVIDER: "wmo",
                CONF_SOURCE_ID: source_id,
                CONF_TRACKER_ENTITY: user_input[CONF_TRACKER_ENTITY],
            }
            return self.async_update_and_abort(
                entry, data=new_data, title=_compute_device_title(new_data)
            )
        return self.async_show_form(
            step_id="reconfigure_wmo_gps_tracker",
            data_schema=_tracker_schema(
                default=entry.data.get(CONF_TRACKER_ENTITY, "")
            ),
        )

    async def async_step_reconfigure_wmo_geocode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch to country-wide fetch narrowed by area-code prefixes."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        source_id = getattr(self, "_wmo_source_id", "")
        if user_input is not None:
            prefixes, err = _validate_geocode_prefixes(
                str(user_input.get(CONF_GEOCODE_PREFIXES, "") or "")
            )
            if err or not prefixes:
                errors["base"] = err or "invalid_geocode_prefix"
            else:
                new_data = {CONF_PROVIDER: "wmo", CONF_SOURCE_ID: source_id}
                return self.async_update_and_abort(
                    entry,
                    data=new_data,
                    # Merged, not replaced: `options=` overwrites the whole
                    # mapping, which would silently drop scan_interval,
                    # timeout, and the language selection.
                    options={**entry.options, CONF_GEOCODE_PREFIXES: prefixes},
                    title=_compute_device_title(new_data),
                )
        return self.async_show_form(
            step_id="reconfigure_wmo_geocode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GEOCODE_PREFIXES,
                        default=",".join(
                            entry.options.get(CONF_GEOCODE_PREFIXES, []) or []
                        ),
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
        errors: dict[str, str] = {}
        if user_input is not None:
            prefixes, err = _validate_geocode_prefixes(
                str(user_input.get(CONF_GEOCODE_PREFIXES, "") or "")
            )
            if err:
                errors["base"] = err
            else:
                data = {**user_input}
                # Absent rather than empty when unset, so the coordinator's
                # `.get(...) or []` short-circuits and options stay tidy.
                if prefixes:
                    data[CONF_GEOCODE_PREFIXES] = prefixes
                else:
                    data.pop(CONF_GEOCODE_PREFIXES, None)
                return self.async_create_entry(title="", data=data)

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
            schema[
                vol.Optional(
                    CONF_STREAMING,
                    default=self.config_entry.options.get(CONF_STREAMING, True),
                )
            ] = bool
            schema[
                vol.Optional(
                    CONF_FEED_SOURCE,
                    default=self.config_entry.options.get(
                        CONF_FEED_SOURCE, DEFAULT_FEED_SOURCE
                    ),
                )
            ] = vol.In(["auto", "alertready", "pelmorex"])
        elif provider == "meteoalarm":
            schema[
                vol.Optional(
                    CONF_LANGUAGE,
                    default=self.config_entry.options.get(CONF_LANGUAGE, "auto"),
                )
            ] = vol.In(list(_METEOALARM_LANGUAGES))
        elif provider == "wmo":
            schema[
                vol.Optional(
                    CONF_LANGUAGE,
                    default=self.config_entry.options.get(CONF_LANGUAGE, "auto"),
                )
            ] = _wmo_language_selector()

        # Marine-alert exclusion is only meaningful for providers that classify
        # marine zones (NWS UGC prefixes, ECCC CLC "00…").
        if provider in ("nws", "eccc"):
            schema[
                vol.Optional(
                    CONF_EXCLUDE_MARINE,
                    default=self.config_entry.options.get(CONF_EXCLUDE_MARINE, False),
                )
            ] = bool

        # Area-code narrowing is provider-neutral: every provider populates
        # CAPAlert.geocodes, so this is offered for all of them. Re-rendered
        # from the rejected input rather than the stored value, so a typo is
        # shown back to the user to correct instead of silently reverting.
        stored_prefixes = self.config_entry.options.get(CONF_GEOCODE_PREFIXES) or []
        prefix_default = (
            str(user_input.get(CONF_GEOCODE_PREFIXES, "") or "")
            if user_input is not None
            else ",".join(stored_prefixes)
        )
        schema[vol.Optional(CONF_GEOCODE_PREFIXES, default=prefix_default)] = str

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
