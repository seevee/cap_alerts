"""U.S. Tsunami Warning Centers: setup and reconfigure steps (issue #298)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import CONF_PROVIDER, CONF_PROVINCE, TWC_REGIONS
from ..schema import vol
from .common import ScopedEntryFlowMixin


def _region_selector() -> SelectSelector:
    """Dropdown of the domestic regions, in ``TWC_REGIONS`` order."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=code, label=label)
                for code, (label, _slots) in TWC_REGIONS.items()
            ],
            mode=SelectSelectorMode.DROPDOWN,
            sort=False,
        )
    )


def _region_schema(default: str | None = None) -> vol.Schema:
    """Schema with the single region dropdown.

    Stored under ``CONF_PROVINCE``, as the AU state is, so the scope key, the
    entry title and diagnostics need no new case.
    """
    if default is not None:
        key: Any = vol.Required(CONF_PROVINCE, default=default)
    else:
        key = vol.Required(CONF_PROVINCE)
    return vol.Schema({key: _region_selector()})


class TWCFlowMixin(ScopedEntryFlowMixin):
    """Tsunami steps, mixed into the domain's flow handler."""

    # ── Tsunami setup ──

    async def async_step_twc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tsunami location scope menu.

        One mode only: a region. The bulletins name the threatened coast in
        text and UGC zones and carry no shape, so there is nothing a GPS
        point could be tested against.
        """
        return self.async_show_menu(
            step_id="twc",
            # "user" is the back edge (issue #140); see the NWS menu.
            menu_options=["twc_region", "user"],
        )

    async def async_step_twc_region(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # No validator: the dropdown is closed, so the schema itself rejects
        # anything outside ``TWC_REGIONS`` before the step sees it.
        if user_input is not None:
            data = {CONF_PROVIDER: "twc", CONF_PROVINCE: user_input[CONF_PROVINCE]}
            return await self._async_create_scoped_entry(data)
        return self.async_show_form(step_id="twc_region", data_schema=_region_schema())

    # ── Tsunami reconfigure ──

    async def async_step_reconfigure_twc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure_twc",
            menu_options=["reconfigure_twc_region", "reconfigure"],
        )

    async def async_step_reconfigure_twc_region(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {CONF_PROVIDER: "twc", CONF_PROVINCE: user_input[CONF_PROVINCE]}
            return await self._async_update_scoped_entry(entry, new_data)
        return self.async_show_form(
            step_id="reconfigure_twc_region",
            data_schema=_region_schema(entry.data.get(CONF_PROVINCE) or None),
        )
