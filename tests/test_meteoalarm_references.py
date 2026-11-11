"""MeteoAlarm revision chains: an Update replaces the message it references.

The fixture is IPMA's 21:23Z orange rain warning for Lisboa, captured from
``feeds-portugal`` on 2026-11-11. It names the 16:30Z message it replaced in
``references``. That predecessor had already left the feed, so the test
derives it from the successor: same warning, earlier identifier and window.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.cap_alerts.const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)
from custom_components.cap_alerts.normalize import normalize_alerts
from custom_components.cap_alerts.providers import meteoalarm
from custom_components.cap_alerts.store import AlertStore

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "meteoalarm_pt_update_chain.json"
)

_SUCCESSOR = "2.49.0.0.620.0.PT.261110212300.PO-10-6288"
_PREDECESSOR = "2.49.0.0.620.0.PT.261110163001.PO-10-6279"


@pytest.fixture
def successor() -> dict:
    feed = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return feed["warnings"][0]


@pytest.fixture
def predecessor(successor) -> dict:
    warning = copy.deepcopy(successor)
    alert = warning["alert"]
    alert["identifier"] = _PREDECESSOR
    alert["sent"] = "2026-11-10T16:30:01+00:00"
    alert["msgType"] = "Alert"
    del alert["references"]
    for info in alert["info"]:
        info["expires"] = "2026-11-11T12:00:00+00:00"
    warning["uuid"] = "predecessor"
    return warning


def _alerts(*warnings: dict):
    parsed = [meteoalarm._warning_to_alert(w, "en") for w in warnings]
    return normalize_alerts([a for a in parsed if a is not None])


def test_update_carries_the_identifier_it_references(successor):
    (alert,) = _alerts(successor)
    assert alert.identifier == _SUCCESSOR
    assert alert.references == (_PREDECESSOR,)


def test_references_as_a_list_parse_the_same(successor):
    """Every feed serves a string today; a list must not fail the fetch."""
    warning = copy.deepcopy(successor)
    warning["alert"]["references"] = [warning["alert"]["references"]]
    (alert,) = _alerts(warning)
    assert alert.references == (_PREDECESSOR,)


def test_new_alert_carries_no_references(predecessor):
    (alert,) = _alerts(predecessor)
    assert alert.references == ()


def test_superseded_message_leaves_instead_of_going_stale(
    freezer, predecessor, successor
):
    """Both messages unexpired, the old one gone from the feed: it must not linger.

    Before references were parsed the store saw only an absence, and
    MeteoAlarm retains on absence, so 6279 sat beside 6288 marked stale
    until its own expiry (the dev box, 2026-11-11).
    """
    freezer.move_to("2026-11-11T01:00:00+00:00")
    fire = MagicMock()
    store = AlertStore(
        "entry1", "meteoalarm", fire=fire, lookup_entity_id=lambda _id: None
    )

    (old,) = store.process(_alerts(predecessor))
    fire.reset_mock()

    result = store.process(_alerts(successor))

    assert [a.identifier for a in result] == [_SUCCESSOR]
    fired = [call.args[0] for call in fire.call_args_list]
    assert fired == [EVENT_INCIDENT_UPDATED]
    assert EVENT_INCIDENT_CREATED not in fired
    assert EVENT_INCIDENT_REMOVED not in fired
    assert result[0].id != old.id
