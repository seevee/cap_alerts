"""The context a cycle's events and state writes carry (issue #310).

Activity resolves a state change to its cause through the context it was
written under: the first row logbook saw with that context. Every write used
to carry a fresh anonymous context, so even a manual refresh read as "No cause
was recorded". Now one context spans a cycle, the events fired before the
writes share it, the ``incident_created`` released after them gets one of its
own, and a refresh button press lends the cycle the user's.

Run against a real Home Assistant instance on a polling ECCC entry, whose
alert id hashes ``sent`` and the polygon rather than the identifier, so a
revision at a new URL with the same ``sent`` is an in-place update.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.cap_alerts.coordinator import AlertsDataUpdateCoordinator

DOMAIN = "cap_alerts"
FEED = "https://rss.alertready.ca/"
SENT = "2026-10-20T12:00:00-00:00"


def _cap_xml(
    identifier: str, event: str, *, severity: str = "Moderate", expires_h: int = 24
) -> str:
    now = datetime.now(timezone.utc)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        f"<sender>CWTO</sender><sent>{SENT}</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        f"<event>{event}</event><urgency>Immediate</urgency>"
        f"<severity>{severity}</severity><certainty>Likely</certainty>"
        f"<expires>{(now + timedelta(hours=expires_h)).isoformat()}</expires>"
        f"<headline>{event} in effect</headline><description>desc</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


def _atom(*cap_urls: str) -> str:
    entries = "".join(
        (
            "<entry>"
            f"<id>atom-{i}</id><title>Warning</title>"
            '<category term="status=Actual"/>'
            "<georss:polygon>45.0 -76.0 45.0 -75.5 45.5 -75.5 45.5 -76.0 "
            "45.0 -76.0</georss:polygon>"
            f'<link type="application/cap+xml" href="{url}"/>'
            "</entry>"
        )
        for i, url in enumerate(cap_urls)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:georss="http://www.georss.org/georss">'
        f"{entries}</feed>"
    )


async def _setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ECCC: Ontario",
        data={"provider": "eccc", "province": "ON"},
        options={"streaming": False, "scan_interval": 300},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass, entry, suffix: str, platform: str = "sensor") -> str:
    eid = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{suffix}"
    )
    assert eid is not None
    return eid


def _alert_entity_ids(hass, entry) -> list[str]:
    prefix = f"{entry.entry_id}_eccc_"
    return [
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(prefix)
    ]


async def _poll(hass, freezer, seconds: int = 360) -> None:
    """Bring the next scheduled poll forward and let it finish.

    The interval refresh runs as a background task, which the default
    ``async_block_till_done`` does not wait for.
    """
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.asyncio
async def test_one_cycle_one_context_across_the_entities(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))

    entry = await _setup(hass)
    count_id = _entity_id(hass, entry, "count")
    last_updated_id = _entity_id(hass, entry, "last_updated")
    (alert_id,) = _alert_entity_ids(hass, entry)
    # The writes on being added take no context: logbook never shows a first
    # state, and one would only serve as a false origin for the boot cycle.
    at_boot = {
        hass.states.get(e).context.id for e in (count_id, last_updated_id, alert_id)
    }
    assert len(at_boot) == 3

    # A quiet poll: the count's state and attributes are unchanged, so it
    # is not rewritten; the timestamp and the alert's ``last_confirmed`` are.
    # Only the timestamp takes the cycle's context: Activity shows that row
    # and no other, so it is the origin and reads as uncaused, the same as
    # any polled sensor in core.
    await _poll(hass, freezer)
    cycle = hass.states.get(last_updated_id).context.id
    assert cycle not in at_boot
    assert hass.states.get(count_id).context.id in at_boot
    alert_write = hass.states.get(alert_id).context.id
    assert alert_write not in at_boot and alert_write != cycle

    # The next poll is a new cycle, so a new context.
    await _poll(hass, freezer)
    assert hass.states.get(last_updated_id).context.id != cycle


@pytest.mark.asyncio
async def test_created_fires_under_its_own_context(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """``incident_created`` fires after the count sensor wrote (issue #249).

    Logbook attributes a row to the first row with its context, so sharing the
    cycle's would read the alert's issue as caused by the count changing. Its
    own context, with no parent, leaves both rows without a cause instead.
    """
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    created = async_capture_events(hass, "incident_created")

    entry = await _setup(hass)

    assert len(created) == 1
    count = hass.states.get(_entity_id(hass, entry, "count"))
    assert created[0].context.id != count.context.id
    assert created[0].context.parent_id is None


@pytest.mark.asyncio
async def test_hidden_rows_take_no_cycle_context(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """The one row Activity shows must not trace to a hidden write.

    Logbook shows no rows for the count sensor (a state class) or an alert
    entity (a device class it takes as numeric), but still memoizes their
    writes as a context's first row, and listeners run in no promised order:
    the floor release writes the count before the timestamp. So on a cycle
    that only added alerts, where nothing fires before the writes, neither
    takes the context, and the last-updated row reads as uncaused.
    """
    cap_a = "https://cap.example/a.cap"
    cap_b = "https://cap.example/b.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    entry = await _setup(hass)
    count_id = _entity_id(hass, entry, "count")
    last_updated_id = _entity_id(hass, entry, "last_updated")
    (alert_a,) = _alert_entity_ids(hass, entry)

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(cap_a, cap_b))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    aioclient_mock.get(
        cap_b,
        text=_cap_xml("urn:oid:B", "Rainfall Warning").replace(
            "45.5,-75.5", "45.6,-75.5"
        ),
    )
    await _poll(hass, freezer)

    assert hass.states.get(count_id).state == "2"
    cycle = hass.states.get(last_updated_id).context.id
    assert hass.states.get(count_id).context.id != cycle
    for alert_id in _alert_entity_ids(hass, entry):
        assert hass.states.get(alert_id).context.id != cycle
    assert alert_a in _alert_entity_ids(hass, entry)


@pytest.mark.asyncio
async def test_updated_is_the_cause_of_the_writes_that_follow(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    cap_a = "https://cap.example/a.cap"
    cap_b = "https://cap.example/b.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    entry = await _setup(hass)
    (alert_id,) = _alert_entity_ids(hass, entry)
    first_cycle = hass.states.get(alert_id).context.id

    # A revision with the same identity (sender, sent, polygon) at a new URL.
    updated = async_capture_events(hass, "incident_updated")
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(cap_b))
    aioclient_mock.get(
        cap_b, text=_cap_xml("urn:oid:B", "Wind Warning", severity="Severe")
    )
    await _poll(hass, freezer)

    assert len(updated) == 1
    assert "severity_normalized" in updated[0].data["changed_fields"]
    alert = hass.states.get(alert_id)
    assert alert.state == "severe"
    # Its own row is hidden (a live alert entity is continuous to logbook),
    # so the write takes no context; the event is the origin either way.
    assert alert.context.id != updated[0].context.id
    assert hass.states.get(_entity_id(hass, entry, "last_updated")).context.id == (
        updated[0].context.id
    )
    assert updated[0].context.id != first_cycle


@pytest.mark.asyncio
async def test_removed_is_the_cause_of_the_writes_that_follow(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """The timestamp's row traces to it; the count's hidden write takes nothing."""
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning", expires_h=1))
    entry = await _setup(hass)
    count_id = _entity_id(hass, entry, "count")
    last_updated_id = _entity_id(hass, entry, "last_updated")
    assert hass.states.get(count_id).state == "1"

    removed = async_capture_events(hass, "incident_removed")
    await _poll(hass, freezer, seconds=2 * 3600)

    assert len(removed) == 1
    assert hass.states.get(count_id).state == "0"
    assert hass.states.get(last_updated_id).context.id == removed[0].context.id
    assert hass.states.get(count_id).context.id != removed[0].context.id


@pytest.mark.asyncio
async def test_refresh_button_lends_the_cycle_the_users_context(
    hass, hass_admin_user, aioclient_mock, enable_custom_integrations, freezer
):
    """The writes carry the press's context; the created event names it as parent.

    That is what makes the dialog read "triggered by action button.press" with
    the user's name on both the last-updated change and the alert's issue.
    """
    cap_a = "https://cap.example/a.cap"
    cap_b = "https://cap.example/b.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    entry = await _setup(hass)

    created = async_capture_events(hass, "incident_created")
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(cap_a, cap_b))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    # A different ``sent`` would be a different id; a different polygon is
    # the cheaper way to a second alert here.
    aioclient_mock.get(
        cap_b,
        text=_cap_xml("urn:oid:B", "Rainfall Warning").replace(
            "45.5,-75.5", "45.6,-75.5"
        ),
    )
    # Under the frozen clock the fetch would stamp the same instant as setup
    # did, and an unchanged timestamp is not rewritten.
    freezer.tick(timedelta(seconds=30))
    press = Context(user_id=hass_admin_user.id)
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _entity_id(hass, entry, "refresh", "button")},
        blocking=True,
        context=press,
    )
    await hass.async_block_till_done()

    assert hass.states.get(_entity_id(hass, entry, "count")).state == "2"
    assert hass.states.get(_entity_id(hass, entry, "last_updated")).context.id == (
        press.id
    )
    assert len(created) == 1
    assert created[0].context.id != press.id
    assert created[0].context.parent_id == press.id

    # The lent context is spent: the next poll is anonymous again.
    await _poll(hass, freezer)
    assert hass.states.get(_entity_id(hass, entry, "last_updated")).context.id != (
        press.id
    )


@pytest.mark.asyncio
async def test_a_failed_refresh_is_its_own_cycle(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """The unavailable writes must not trace to the last good cycle's events."""
    cap_a = "https://cap.example/a.cap"
    aioclient_mock.get(FEED, text=_atom(cap_a))
    aioclient_mock.get(cap_a, text=_cap_xml("urn:oid:A", "Wind Warning"))
    entry = await _setup(hass)
    count_id = _entity_id(hass, entry, "count")
    good_cycle = hass.states.get(count_id).context.id

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, status=503)
    await _poll(hass, freezer)

    count = hass.states.get(count_id)
    assert count.state == STATE_UNAVAILABLE
    assert count.context.id != good_cycle


# --- the coordinator's bookkeeping, without Home Assistant -----------------


def _bare_coordinator() -> AlertsDataUpdateCoordinator:
    coordinator = object.__new__(AlertsDataUpdateCoordinator)
    coordinator.hass = MagicMock()
    coordinator._listeners = {}
    coordinator._cycle_context = Context()
    return coordinator


def _fired_context(coordinator) -> Context:
    return coordinator.hass.bus.async_fire.call_args.kwargs["context"]


def test_an_event_before_the_writes_shares_the_cycle_context():
    coordinator = _bare_coordinator()
    coordinator._begin_cycle()

    coordinator._async_fire_event("incident_removed", {"incident_id": "a"})

    assert _fired_context(coordinator) is coordinator.cycle_context


def test_an_event_after_the_writes_gets_its_own_context():
    coordinator = _bare_coordinator()
    coordinator._begin_cycle()
    coordinator.async_update_listeners()

    coordinator._async_fire_event("incident_created", {"incident_id": "a"})

    context = _fired_context(coordinator)
    assert context is not coordinator.cycle_context
    assert context.parent_id is None


def test_a_connection_notification_begins_a_cycle_of_its_own():
    coordinator = _bare_coordinator()
    coordinator._begin_cycle()
    before = coordinator.cycle_context

    coordinator.notify_connection(False)

    assert coordinator.cycle_context is not before
