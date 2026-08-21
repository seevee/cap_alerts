"""Repairs issues an entry owes because of its configuration (issue #163).

The legacy NAAD GeoRSS host, ``rss.naad-adna.pelmorex.com``, retires around
late September 2026, and two ECCC configurations depend on it without being
told so today:

- **streaming off**: the entry polls the GeoRSS host union (#38), which
  degrades to ``rss.alertready.ca`` alone at the sunset. That host has never
  carried every live alert (SR #46534, abandoned upstream), and for a polling
  entry an alert the index drops mid-lifetime is read by ``AlertStore`` as
  ended — a false all-clear, the failure the feed guard exists to prevent.
- **feed source pinned to pelmorex**: every GeoRSS fetch fails. A polling
  entry goes unavailable; a streaming entry loses its backfill and resync.

Both are raised from the configuration alone, whenever it matches, with no
sunset detection: the union is already the only thing making the index
complete, so each recommendation is correct today, and a detector would buy a
few weeks of silence in exchange for a state machine. The lifecycle is
idempotent and config-driven — re-evaluated at setup, on every entry update,
and on removal.

This module touches only ``homeassistant.helpers.issue_registry`` so that
``__init__`` can call it at setup without importing the ``repairs`` component.
The confirm flows that apply each recommendation live in ``repairs.py``, the
platform file Home Assistant loads lazily when a card's fix button is pressed.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_FEED_SOURCE,
    CONF_PROVIDER,
    CONF_STREAMING,
    DEFAULT_FEED_SOURCE,
    DOMAIN,
    ISSUE_ECCC_FEED_SOURCE_PELMOREX,
    ISSUE_ECCC_STREAMING_OFF,
    ISSUE_LEARN_MORE_URL,
)

ISSUE_STEMS: tuple[str, ...] = (
    ISSUE_ECCC_STREAMING_OFF,
    ISSUE_ECCC_FEED_SOURCE_PELMOREX,
)


def issue_id(stem: str, entry: ConfigEntry) -> str:
    """The registry id for one stem on one entry."""
    return f"{stem}_{entry.entry_id}"


@callback
def async_sync_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise or clear the ECCC sunset repairs for one entry from its config."""
    eccc = entry.data.get(CONF_PROVIDER) == "eccc"
    _sync(
        hass,
        entry,
        ISSUE_ECCC_STREAMING_OFF,
        eccc and not entry.options.get(CONF_STREAMING, True),
    )
    _sync(
        hass,
        entry,
        ISSUE_ECCC_FEED_SOURCE_PELMOREX,
        eccc and entry.options.get(CONF_FEED_SOURCE, DEFAULT_FEED_SOURCE) == "pelmorex",
    )


@callback
def async_delete_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear every issue this module may have raised for ``entry``.

    Issues are non-persistent, so a restart starts clean and setup recreates
    whatever still applies; this is for an entry removed mid-session, whose
    cards would otherwise outlive it.
    """
    for stem in ISSUE_STEMS:
        ir.async_delete_issue(hass, DOMAIN, issue_id(stem, entry))


@callback
def _sync(hass: HomeAssistant, entry: ConfigEntry, stem: str, owed: bool) -> None:
    if not owed:
        ir.async_delete_issue(hass, DOMAIN, issue_id(stem, entry))
        return
    # Creating an existing id updates it in place, so a retitled entry
    # refreshes its card rather than growing a second one.
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(stem, entry),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        issue_domain=DOMAIN,
        learn_more_url=ISSUE_LEARN_MORE_URL,
        severity=ir.IssueSeverity.WARNING,
        translation_key=stem,
        translation_placeholders={"title": entry.title},
    )
