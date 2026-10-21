"""The six incident triggers (issues #280, #326).

``tests/test_device_class_binding.py`` pins why core's entity-state helper was
not the tool: it fires on a change, and an alert that arrives severe has no
previous state. These pin the triggers the integration ships instead, over the
bus events, on a real polling ECCC entry: what fires each one, what doesn't,
how the threshold reads, and that the editor can list them.

The feed-driven tests prove the wiring from a poll through the store to the
automation. The matrix tests fire the store's payload shape on the bus
directly, so they stay cheap and say which rung decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import trigger as trigger_helper
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.cap_alerts.const import DOMAIN
from custom_components.cap_alerts.trigger import THRESHOLD_RANK, TRIGGERS
from tests.test_activity_context import (
    FEED,
    SENT,
    _alert_entity_ids,
    _atom,
    _cap_xml,
    _poll,
)
from tests.test_activity_context import _setup as _setup_entry

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "cap_alerts"
FIRED = "incident_trigger_fired"
CAP_A = "https://cap.example/a.cap"
CAP_B = "https://cap.example/b.cap"


async def _setup(hass: HomeAssistant, aioclient_mock, *, severity: str = "Moderate"):
    """One live Wind Warning on an ECCC polling entry; returns (entry, entity_id)."""
    aioclient_mock.get(FEED, text=_atom(CAP_A))
    aioclient_mock.get(
        CAP_A, text=_cap_xml("urn:oid:A", "Wind Warning", severity=severity)
    )
    entry = await _setup_entry(hass)
    (entity_id,) = _alert_entity_ids(hass, entry)
    return entry, entity_id


async def _arm(
    hass: HomeAssistant,
    trigger: str,
    target: dict,
    threshold: str | None = None,
) -> list:
    """Attach one automation on ``trigger`` and capture what it fires."""
    fired = async_capture_events(hass, FIRED)
    config: dict = {"trigger": f"{DOMAIN}.{trigger}", "target": target}
    if threshold is not None:
        config["options"] = {"threshold": threshold}
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": config,
                "action": {
                    "event": FIRED,
                    "event_data": {
                        "entity_id": "{{ trigger.entity_id }}",
                        "severity": "{{ trigger.severity }}",
                        "previous": "{{ trigger.previous_severity | default('') }}",
                        "reason": "{{ trigger.removal_reason | default('') }}",
                    },
                },
            }
        },
    )
    return fired


def _device_target(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {"device_id": device.id}


async def _fire(hass: HomeAssistant, event_type: str, entity_id: str, **data) -> None:
    """Put an event of the store's shape on the bus and let the automation run."""
    payload = {
        "entry_id": "entry",
        "incident_id": "urn:oid:A",
        "event": "Wind Warning",
        "severity": "moderate",
        "phase": "new",
        "phase_changed": False,
        "changed_fields": [],
        "area_desc": "Ottawa",
        "entity_id": entity_id,
    }
    payload.update(data)
    hass.bus.async_fire(event_type, payload)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Through the feed: a poll, the store, the event, the automation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_alert_that_arrives_severe_fires_on_a_device_target(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """The case the state helper could not do: no previous state, still fires.

    The device target is set before the alert exists. The tracker picks the
    entity up as it registers, ahead of the ``incident_created`` the entity
    releases once added.
    """
    entry, _ = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_arrived", _device_target(hass, entry))

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_A, CAP_B))
    aioclient_mock.get(CAP_A, text=_cap_xml("urn:oid:A", "Wind Warning"))
    # ECCC identity hashes ``sent`` and the polygon, so a second alert needs its
    # own ``sent`` or the fixture collapses the two into one.
    aioclient_mock.get(
        CAP_B,
        text=_cap_xml("urn:oid:B", "Tornado Warning", severity="Extreme").replace(
            SENT, "2026-10-20T13:00:00-00:00"
        ),
    )
    await _poll(hass, freezer)

    new_ids = set(_alert_entity_ids(hass, entry))
    assert len(new_ids) == 2
    assert [e.data["severity"] for e in fired] == ["extreme"]
    assert fired[0].data["entity_id"] in new_ids
    assert hass.states.get(fired[0].data["entity_id"]).state == "extreme"


@pytest.mark.asyncio
async def test_an_escalation_in_the_feed_fires_once_and_names_the_rungs(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    entry, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_escalated", _device_target(hass, entry))

    # Same identity (sender, sent, polygon) at a new URL, one rung up.
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_B))
    aioclient_mock.get(
        CAP_B, text=_cap_xml("urn:oid:B", "Wind Warning", severity="Severe")
    )
    await _poll(hass, freezer)

    assert [e.data for e in fired] == [
        {
            "entity_id": entity_id,
            "severity": "severe",
            "previous": "moderate",
            "reason": "",
        }
    ]


@pytest.mark.asyncio
async def test_a_downgrade_in_the_feed_fires_the_mirror(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    entry, entity_id = await _setup(hass, aioclient_mock, severity="Severe")
    down = await _arm(hass, "incident_downgraded", _device_target(hass, entry))

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_B))
    aioclient_mock.get(
        CAP_B, text=_cap_xml("urn:oid:B", "Wind Warning", severity="Moderate")
    )
    await _poll(hass, freezer)

    assert [e.data for e in down] == [
        {
            "entity_id": entity_id,
            "severity": "moderate",
            "previous": "severe",
            "reason": "",
        }
    ]
    assert hass.states.get(entity_id).state == "moderate"


@pytest.mark.asyncio
async def test_an_ending_in_the_feed_fires_with_its_reason(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    entry, entity_id = await _setup(hass, aioclient_mock, severity="Severe")
    fired = await _arm(hass, "incident_ended", _device_target(hass, entry))

    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_B))
    aioclient_mock.get(
        CAP_B,
        text=_cap_xml("urn:oid:B", "Wind Warning", severity="Severe", expires_h=-1),
    )
    await _poll(hass, freezer)

    assert [e.data["entity_id"] for e in fired] == [entity_id]
    assert fired[0].data["severity"] == "severe"
    assert hass.states.get(entity_id) is None


# ---------------------------------------------------------------------------
# The threshold, rung by rung
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "severity", "fires"),
    [
        (None, "severe", True),  # the default is severe
        (None, "moderate", False),
        ("severe", "extreme", True),
        ("minor", "minor", True),
        ("minor", "unknown", False),
        ("any", "unknown", True),
        ("extreme", "severe", False),
    ],
)
async def test_arrived_fires_at_or_above_the_threshold(
    hass, aioclient_mock, enable_custom_integrations, threshold, severity, fires
):
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_arrived", {"entity_id": entity_id}, threshold)

    await _fire(hass, "incident_created", entity_id, severity=severity)

    assert len(fired) == (1 if fires else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "previous", "severity", "fires"),
    [
        ("severe", "moderate", "severe", True),
        ("severe", "minor", "extreme", True),
        ("severe", "severe", "extreme", False),  # already what was asked about
        ("severe", "minor", "moderate", False),  # a rise that stops short
        ("severe", "extreme", "severe", False),  # a fall
        ("any", "unknown", "minor", True),  # any: every rise
        ("any", "severe", "extreme", True),
        ("any", "severe", "moderate", False),
        ("minor", "unknown", "minor", True),
    ],
)
async def test_escalated_fires_on_a_crossing_from_below(
    hass,
    aioclient_mock,
    enable_custom_integrations,
    threshold,
    previous,
    severity,
    fires,
):
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_escalated", {"entity_id": entity_id}, threshold)

    await _fire(
        hass,
        "incident_updated",
        entity_id,
        severity=severity,
        previous_severity=previous,
        changed_fields=["severity_normalized"],
    )

    assert len(fired) == (1 if fires else 0)


@pytest.mark.asyncio
async def test_escalated_ignores_an_update_that_left_severity_alone(
    hass, aioclient_mock, enable_custom_integrations
):
    """A reworded headline on a severe alert is not an escalation."""
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_escalated", {"entity_id": entity_id}, "any")

    await _fire(
        hass,
        "incident_updated",
        entity_id,
        severity="severe",
        previous_severity="severe",
        changed_fields=["headline"],
    )

    assert fired == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "previous", "severity", "fires"),
    [
        ("severe", "severe", "moderate", True),
        ("severe", "extreme", "minor", True),
        ("severe", "extreme", "severe", False),  # still what was asked about
        ("severe", "moderate", "minor", False),  # a fall that was already below
        ("severe", "moderate", "severe", False),  # a rise
        ("any", "minor", "unknown", True),  # any: every fall
        ("any", "extreme", "severe", True),
        ("any", "severe", "extreme", False),
        ("minor", "minor", "unknown", True),
    ],
)
async def test_downgraded_fires_on_a_crossing_from_above(
    hass,
    aioclient_mock,
    enable_custom_integrations,
    threshold,
    previous,
    severity,
    fires,
):
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_downgraded", {"entity_id": entity_id}, threshold)

    await _fire(
        hass,
        "incident_updated",
        entity_id,
        severity=severity,
        previous_severity=previous,
        changed_fields=["severity_normalized"],
    )

    assert len(fired) == (1 if fires else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "severity", "changed", "fires"),
    [
        (None, "severe", ["headline"], True),  # any field, default threshold
        (None, "severe", ["expires"], True),  # an extended expiry counts
        (None, "moderate", ["headline"], False),
        ("severe", "moderate", ["severity_normalized"], False),  # a fall out of range
        ("any", "unknown", ["description"], True),
    ],
)
async def test_updated_fires_on_any_revision_at_or_above(
    hass,
    aioclient_mock,
    enable_custom_integrations,
    threshold,
    severity,
    changed,
    fires,
):
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_updated", {"entity_id": entity_id}, threshold)

    await _fire(
        hass,
        "incident_updated",
        entity_id,
        severity=severity,
        previous_severity="severe",
        changed_fields=changed,
    )

    assert len(fired) == (1 if fires else 0)


@pytest.mark.asyncio
async def test_updated_carries_changed_fields_for_a_condition(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """The feed-driven case: a reworded revision, and a condition sees what moved."""
    entry, _ = await _setup(hass, aioclient_mock, severity="Severe")
    fired = async_capture_events(hass, FIRED)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {
                    "trigger": f"{DOMAIN}.incident_updated",
                    "target": _device_target(hass, entry),
                },
                "condition": "{{ 'headline' in trigger.changed_fields }}",
                "action": {
                    "event": FIRED,
                    "event_data": {
                        "changed": "{{ trigger.changed_fields | join(',') }}"
                    },
                },
            }
        },
    )

    # Same identity at a new URL; the headline follows the event text.
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_B))
    aioclient_mock.get(
        CAP_B, text=_cap_xml("urn:oid:B", "Wind Warning Revised", severity="Severe")
    )
    await _poll(hass, freezer)

    assert len(fired) == 1
    assert "headline" in fired[0].data["changed"].split(",")
    assert "severity_normalized" not in fired[0].data["changed"]


@pytest.mark.asyncio
async def test_escalated_and_downgraded_never_fire_on_the_same_update(
    hass, aioclient_mock, enable_custom_integrations
):
    """Two automations on one entity, one per direction, one update each way."""
    _, entity_id = await _setup(hass, aioclient_mock)
    both = async_capture_events(hass, FIRED)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": direction,
                    "trigger": {
                        "trigger": f"{DOMAIN}.incident_{direction}",
                        "target": {"entity_id": entity_id},
                        "options": {"threshold": "any"},
                    },
                    "action": {"event": FIRED, "event_data": {"which": direction}},
                }
                for direction in ("escalated", "downgraded")
            ]
        },
    )

    await _fire(
        hass,
        "incident_updated",
        entity_id,
        severity="severe",
        previous_severity="minor",
        changed_fields=["severity_normalized"],
    )
    await _fire(
        hass,
        "incident_updated",
        entity_id,
        severity="moderate",
        previous_severity="severe",
        changed_fields=["severity_normalized"],
    )

    assert [e.data["which"] for e in both] == ["escalated", "downgraded"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "severity", "fires"),
    [
        (None, "severe", True),
        (None, "moderate", False),
        ("any", "unknown", True),  # a removal announced from the registry
        ("minor", "unknown", False),
    ],
)
async def test_ended_fires_at_or_above_the_threshold(
    hass, aioclient_mock, enable_custom_integrations, threshold, severity, fires
):
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_ended", {"entity_id": entity_id}, threshold)

    await _fire(
        hass,
        "incident_removed",
        entity_id,
        severity=severity,
        phase="cancel",
        removal_reason="ended",
    )

    assert [e.data["reason"] for e in fired] == (["ended"] if fires else [])


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_alert_outside_the_target_does_not_fire(
    hass, aioclient_mock, enable_custom_integrations
):
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(
        hass, "incident_arrived", {"entity_id": "sensor.elsewhere"}, "any"
    )

    await _fire(hass, "incident_created", entity_id, severity="extreme")
    # And an event with no entity at all, a first sighting already terminal.
    hass.bus.async_fire("incident_created", {"incident_id": "x", "severity": "extreme"})
    await hass.async_block_till_done()

    assert fired == []


@pytest.mark.asyncio
async def test_a_trigger_without_a_target_is_refused(
    hass, aioclient_mock, enable_custom_integrations, caplog
):
    await _setup(hass, aioclient_mock)

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {"trigger": f"{DOMAIN}.incident_arrived"},
                "action": {"event": FIRED},
            }
        },
    )
    await hass.async_block_till_done()

    assert "required key not provided" in caplog.text
    assert "target" in caplog.text


@pytest.mark.asyncio
async def test_disarming_stops_the_listener(
    hass, aioclient_mock, enable_custom_integrations
):
    _, entity_id = await _setup(hass, aioclient_mock)
    fired = await _arm(hass, "incident_arrived", {"entity_id": entity_id}, "any")
    (automation,) = [s.entity_id for s in hass.states.async_all("automation")]

    await hass.services.async_call(
        "automation", "turn_off", {"entity_id": automation}, blocking=True
    )
    await _fire(hass, "incident_created", entity_id, severity="extreme")

    assert fired == []


# ---------------------------------------------------------------------------
# What the editor sees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_editor_gets_the_incident_target_and_the_select(
    hass, aioclient_mock, enable_custom_integrations, caplog
):
    _, entity_id = await _setup(hass, aioclient_mock)
    await _arm(hass, "incident_arrived", {"entity_id": entity_id})

    descriptions = await trigger_helper.async_get_all_descriptions(hass)

    for key in TRIGGERS:
        description = descriptions[f"{DOMAIN}.{key}"]
        assert description["target"] == {
            "entity": [{"domain": ["sensor"], "device_class": ["incident"]}]
        }
        select = description["fields"]["threshold"]["selector"]["select"]
        assert select["options"] == list(THRESHOLD_RANK)
        assert description["fields"]["threshold"]["default"] == "severe"
    assert "triggers.yaml" not in caplog.text


def test_every_trigger_is_described_and_named():
    """The editor lists a trigger only with a triggers.yaml row and strings."""
    described = yaml.safe_load((_PKG / "triggers.yaml").read_text(encoding="utf-8"))
    # Core drops the dotted anchor keys before it reads the file.
    assert {key for key in described if not key.startswith(".")} == set(TRIGGERS)
    for path in (_PKG / "strings.json", _PKG / "translations" / "en.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        strings = data["triggers"]
        assert set(strings) == set(TRIGGERS), path.name
        for key, value in strings.items():
            assert value["name"] and value["description"], f"{path.name}: {key}"
            field = value["fields"]["threshold"]
            assert field["name"] and field["description"], f"{path.name}: {key}"
        options = data["selector"]["trigger_threshold"]["options"]
        assert set(options) == set(THRESHOLD_RANK), path.name


def test_the_threshold_axis_is_the_severity_axis():
    """``any`` stands where ``unknown`` ranks, under ``minor``."""
    assert THRESHOLD_RANK["any"] == 0
    assert THRESHOLD_RANK["any"] < THRESHOLD_RANK["minor"] < THRESHOLD_RANK["extreme"]
    assert "unknown" not in THRESHOLD_RANK


# ---------------------------------------------------------------------------
# The timed trigger (#326): a timer per alert on onset
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    async_fire_time_changed,
)


def _timed_xml(
    identifier: str,
    *,
    onset: datetime | None,
    expires: datetime,
    severity: str = "Severe",
    event: str = "Winter Storm Warning",
) -> str:
    """An ECCC alert with explicit onset and expires, same identity as the fixture."""
    onset_tag = f"<onset>{onset.isoformat()}</onset>" if onset else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        f"<sender>CWTO</sender><sent>{SENT}</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        f"<event>{event}</event><urgency>Immediate</urgency>"
        f"<severity>{severity}</severity><certainty>Likely</certainty>"
        f"{onset_tag}<expires>{expires.isoformat()}</expires>"
        f"<headline>{event} in effect</headline><description>desc</description>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )


async def _setup_timed(hass, aioclient_mock, freezer, **xml) -> tuple:
    """Freeze the clock, serve one timed alert, set the entry up."""
    freezer.move_to("2026-10-20T12:00:00+00:00")
    aioclient_mock.get(FEED, text=_atom(CAP_A))
    aioclient_mock.get(CAP_A, text=_timed_xml("urn:oid:A", **xml))
    entry = await _setup_entry(hass)
    (entity_id,) = _alert_entity_ids(hass, entry)
    return entry, entity_id


async def _advance_to(hass, freezer, when: datetime) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _arm_timed(hass, entry, trigger: str, **options) -> list:
    fired = async_capture_events(hass, FIRED)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {
                    "trigger": f"{DOMAIN}.{trigger}",
                    "target": _device_target(hass, entry),
                    "options": options,
                },
                "action": {
                    "event": FIRED,
                    "event_data": {
                        "entity_id": "{{ trigger.entity_id }}",
                        "event": "{{ trigger.event }}",
                        "severity": "{{ trigger.severity }}",
                        "onset": "{{ trigger.onset | default('') }}",
                        "expires": "{{ trigger.expires | default('') }}",
                    },
                },
            }
        },
    )
    return fired


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_starts_fires_once_when_the_onset_arrives(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    onset = NOW + timedelta(hours=6)
    entry, entity_id = await _setup_timed(
        hass, aioclient_mock, freezer, onset=onset, expires=NOW + timedelta(days=1)
    )
    fired = await _arm_timed(hass, entry, "incident_started")

    await _advance_to(hass, freezer, onset - timedelta(minutes=1))
    assert fired == []
    await _advance_to(hass, freezer, onset + timedelta(seconds=1))
    # ``event`` is the entity's attribute, which the fixture's Atom title sets.
    assert [e.data for e in fired] == [
        {
            "entity_id": entity_id,
            "event": "Warning",
            "severity": "severe",
            "onset": onset.isoformat(),
            "expires": "",
        }
    ]
    # Later polls rewrite the state with the same onset: no second firing.
    await _advance_to(hass, freezer, onset + timedelta(hours=2))
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_starts_does_not_fire_for_an_onset_already_past(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    """The common case: 61% of ECCC arrivals. Alert issued covered it."""
    entry, _ = await _setup_timed(
        hass,
        aioclient_mock,
        freezer,
        onset=NOW - timedelta(hours=2),
        expires=NOW + timedelta(days=1),
    )
    fired = await _arm_timed(hass, entry, "incident_started", threshold="any")

    await _advance_to(hass, freezer, NOW + timedelta(hours=12))
    assert fired == []


@pytest.mark.asyncio
async def test_starts_honours_an_offset_before(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    onset = NOW + timedelta(hours=6)
    entry, _ = await _setup_timed(
        hass, aioclient_mock, freezer, onset=onset, expires=NOW + timedelta(days=1)
    )
    fired = await _arm_timed(
        hass, entry, "incident_started", offset={"hours": 1}, offset_type="before"
    )

    await _advance_to(hass, freezer, onset - timedelta(hours=1, minutes=1))
    assert fired == []
    await _advance_to(hass, freezer, onset - timedelta(minutes=59))
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_starts_rearms_when_a_revision_moves_the_onset(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    onset = NOW + timedelta(hours=6)
    entry, _ = await _setup_timed(
        hass, aioclient_mock, freezer, onset=onset, expires=NOW + timedelta(days=1)
    )
    fired = await _arm_timed(hass, entry, "incident_started")

    # A revision pushes the onset back two hours before it arrives.
    moved = onset + timedelta(hours=2)
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_B))
    aioclient_mock.get(
        CAP_B,
        text=_timed_xml("urn:oid:B", onset=moved, expires=NOW + timedelta(days=1)),
    )
    await _poll(hass, freezer)
    assert hass.states.get(_alert_entity_ids(hass, entry)[0]).attributes["onset"] == (
        moved.isoformat()
    )

    await _advance_to(hass, freezer, onset + timedelta(minutes=5))
    assert fired == []
    await _advance_to(hass, freezer, moved + timedelta(seconds=1))
    assert len(fired) == 1
    await _advance_to(hass, freezer, moved + timedelta(hours=1))
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_starts_reads_the_threshold_when_the_time_arrives(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    onset = NOW + timedelta(hours=6)
    entry, _ = await _setup_timed(
        hass,
        aioclient_mock,
        freezer,
        onset=onset,
        expires=NOW + timedelta(days=1),
        severity="Moderate",
    )
    fired = await _arm_timed(hass, entry, "incident_started")  # default: severe

    await _advance_to(hass, freezer, onset + timedelta(seconds=1))
    assert fired == []


@pytest.mark.asyncio
async def test_starts_is_cancelled_when_the_alert_ends_first(
    hass, aioclient_mock, enable_custom_integrations, freezer
):
    onset = NOW + timedelta(hours=6)
    entry, entity_id = await _setup_timed(
        hass, aioclient_mock, freezer, onset=onset, expires=NOW + timedelta(days=1)
    )
    fired = await _arm_timed(hass, entry, "incident_started")

    # Withdrawn before it starts: the feed goes terminal on the alert.
    aioclient_mock.clear_requests()
    aioclient_mock.get(FEED, text=_atom(CAP_B))
    aioclient_mock.get(
        CAP_B,
        text=_timed_xml(
            "urn:oid:B", onset=onset, expires=NOW + timedelta(days=1)
        ).replace("<msgType>Alert</msgType>", "<msgType>Cancel</msgType>"),
    )
    await _poll(hass, freezer)
    assert hass.states.get(entity_id) is None

    await _advance_to(hass, freezer, onset + timedelta(minutes=5))
    assert fired == []


@pytest.mark.asyncio
async def test_the_timed_trigger_describes_its_offset_fields(
    hass, aioclient_mock, enable_custom_integrations
):
    _, entity_id = await _setup(hass, aioclient_mock)
    await _arm(hass, "incident_arrived", {"entity_id": entity_id})

    descriptions = await trigger_helper.async_get_all_descriptions(hass)

    fields = descriptions[f"{DOMAIN}.incident_started"]["fields"]
    assert set(fields) == {"threshold", "offset", "offset_type"}
    assert fields["offset_type"]["default"] == "before"
    assert "duration" in fields["offset"]["selector"]
    for key in ("incident_arrived", "incident_ended"):
        assert set(descriptions[f"{DOMAIN}.{key}"]["fields"]) == {"threshold"}
