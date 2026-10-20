"""The logbook platform: Activity rows for the incident events (issue #310).

Logbook drops an entity's first state and its removal by design, so until the
events were described, an alert appearing or ending left no row at all. The
describers are plain functions over the event payload, tested directly; the
discovery test proves logbook finds the platform in a custom integration.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.core import callback
from homeassistant.helpers.integration_platform import (
    async_process_integration_platforms,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts import logbook
from custom_components.cap_alerts.const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)

DOMAIN = "cap_alerts"


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    """A registered (not set up) NWS entry, enough for the name lookup."""
    e = MockConfigEntry(domain=DOMAIN, data={"provider": "nws", "zone": "COZ040"})
    e.add_to_hass(hass)
    return e


# --- name ----------------------------------------------------------------


def test_name_is_the_device_name_then_the_event(hass, entry):
    data = {"entry_id": entry.entry_id, "event": "Winter Storm Warning"}
    assert logbook.describe_name(hass, data) == "CAP Alerts NWS Winter Storm Warning"


def test_name_is_the_event_alone_once_the_entry_is_gone(hass):
    """Rows outlive entries; a deleted entry's rows still say what the alert was."""
    data = {"entry_id": "gone", "event": "Winter Storm Warning"}
    assert logbook.describe_name(hass, data) == "Winter Storm Warning"


def test_name_is_the_device_alone_when_the_event_is_blank(hass, entry):
    """A removal announced from the registry can carry no event (events.md)."""
    assert logbook.describe_name(hass, {"entry_id": entry.entry_id, "event": ""}) == (
        "CAP Alerts NWS"
    )


def test_name_never_comes_back_empty(hass):
    assert logbook.describe_name(hass, {}) == "CAP Alerts"


# --- message -------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "data", "expected"),
    [
        (EVENT_INCIDENT_CREATED, {"severity": "severe"}, "issued (severe)"),
        (EVENT_INCIDENT_CREATED, {"severity": "unknown"}, "issued"),
        (EVENT_INCIDENT_CREATED, {}, "issued"),
        (
            EVENT_INCIDENT_UPDATED,
            {"changed_fields": ["severity_normalized", "expires"]},
            "updated (severity, expires)",
        ),
        (EVENT_INCIDENT_UPDATED, {"changed_fields": []}, "updated"),
        (EVENT_INCIDENT_UPDATED, {}, "updated"),
        (EVENT_INCIDENT_REMOVED, {"removal_reason": "superseded"}, "superseded"),
        (EVENT_INCIDENT_REMOVED, {"removal_reason": "ended"}, "ended"),
        (EVENT_INCIDENT_REMOVED, {"phase": "cancel"}, "cancelled"),
        (EVENT_INCIDENT_REMOVED, {"phase": "expired"}, "expired"),
        (EVENT_INCIDENT_REMOVED, {}, "expired"),
        # The registry-path removal after a restart (issue #321): phase cancel,
        # severity unknown, no area and no reason. Not a cancellation.
        (
            EVENT_INCIDENT_REMOVED,
            {"phase": "cancel", "severity": "unknown", "area_desc": ""},
            "removed",
        ),
        # An alert the source published without a severity still carries its
        # area, and its silent disappearance still reads as the inference.
        (
            EVENT_INCIDENT_REMOVED,
            {"phase": "cancel", "severity": "unknown", "area_desc": "Clark County"},
            "cancelled",
        ),
        (
            EVENT_INCIDENT_REMOVED,
            {"phase": "cancel", "severity": "unknown", "removal_reason": "ended"},
            "ended",
        ),
    ],
)
def test_message(event_type, data, expected):
    assert logbook.describe_message(event_type, data) == expected


# --- the platform hook ---------------------------------------------------


def _registered(hass) -> dict[str, tuple[str, object]]:
    seen: dict[str, tuple[str, object]] = {}

    def record(domain, event_type, describe):
        seen[event_type] = (domain, describe)

    logbook.async_describe_events(hass, record)
    return seen


def test_describes_all_three_events_under_the_domain(hass):
    seen = _registered(hass)
    assert set(seen) == {
        EVENT_INCIDENT_CREATED,
        EVENT_INCIDENT_UPDATED,
        EVENT_INCIDENT_REMOVED,
    }
    assert {domain for domain, _ in seen.values()} == {DOMAIN}


def test_row_links_the_entity_and_carries_the_context(hass, entry):
    _, describe = _registered(hass)[EVENT_INCIDENT_REMOVED]
    event = SimpleNamespace(
        data={
            "entry_id": entry.entry_id,
            "event": "Wind Warning",
            "phase": "cancel",
            "removal_reason": "ended",
            "entity_id": "sensor.cap_alerts_nws_cap_alert_wind_warning_12345678",
        },
        context_id="ctx-1",
    )
    assert describe(event) == {
        "name": "CAP Alerts NWS Wind Warning",
        "message": "ended",
        "entity_id": "sensor.cap_alerts_nws_cap_alert_wind_warning_12345678",
        "context_id": "ctx-1",
    }


def test_row_without_an_entity_has_no_link(hass, entry):
    """A first sighting already terminal never had an entity (events.md)."""
    _, describe = _registered(hass)[EVENT_INCIDENT_REMOVED]
    row = describe(SimpleNamespace(data={"entry_id": entry.entry_id}, context_id="c"))
    assert row["entity_id"] is None


@pytest.mark.asyncio
async def test_logbook_discovers_the_platform(
    hass, aioclient_mock, enable_custom_integrations
):
    """Logbook finds ``logbook.py`` by name once the integration is loaded.

    Nothing in the manifest declares it, so this is the one check that the
    file is where core's platform discovery looks for a custom integration.
    Logbook itself cannot be set up here (it depends on the frontend package,
    which the test environment doesn't ship), so this runs the discovery
    helper logbook's own setup runs, with the same hook.
    """
    aioclient_mock.get(
        "https://api.weather.gov/alerts/active?zone=COZ040",
        json={"features": []},
    )
    entry = MockConfigEntry(domain=DOMAIN, data={"provider": "nws", "zone": "COZ040"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    external: dict[str, tuple[str, object]] = {}

    @callback
    def _process(hass, domain, platform) -> None:
        platform.async_describe_events(
            hass,
            lambda d, event_type, describe: external.__setitem__(
                event_type, (d, describe)
            ),
        )

    await async_process_integration_platforms(hass, "logbook", _process)

    for event_type in (
        EVENT_INCIDENT_CREATED,
        EVENT_INCIDENT_UPDATED,
        EVENT_INCIDENT_REMOVED,
    ):
        domain, describe = external[event_type]
        assert domain == DOMAIN
        assert describe is not None
