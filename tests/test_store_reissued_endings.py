"""A revised terminal document under a new key is the same ending (#185).

The id-keyed tombstone (issue #145) suppresses a repeated ``incident_removed``
only when the repeat arrives under the *same* id. ECCC has been observed
re-issuing an already-ended group under a fresh revision — new bilingual key,
because ``sent`` is a key input, same ending — with the new document's
``references`` naming the CAP identifier of the one just tombstoned. Polling
never sees this: both revisions land in one scan and ``resolve_chain_leaves``
drops the older one first. Streaming ingests each revision as it arrives, so
these tests feed the store one revision per ``process()`` call, the shape
that exposed the duplicate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _entity_registry_from_mock(monkeypatch):
    """Point ``er.async_get`` at the mock ``hass``'s registry attribute.

    The store looks the registry up through ``er.async_get(hass)``, which reads
    ``hass.data``; the fixture below is a MagicMock, so without this the store
    gets a bare mock and the entity id in the payload is a mock too.
    """
    monkeypatch.setattr(
        "custom_components.cap_alerts.store.er.async_get",
        lambda hass: hass.entity_registry,
    )


@pytest.fixture
def hass():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    h.entity_registry.async_get_entity_id.return_value = None
    return h


def _events(hass) -> list[str]:
    return [call.args[0] for call in hass.bus.async_fire.call_args_list]


def _eccc(alert_factory, **overrides):
    return alert_factory(provider="eccc", **overrides)


def test_reissued_ended_group_fires_one_removal(hass, alert_factory):
    """The reporter's case: same ending, re-issued 24s later under a new key.

    Revision 1 ends the group and is tombstoned by id and identifier.
    Revision 2 is a different id (fresh bilingual key) whose ``references``
    name revision 1's identifier — the same ending, not a new one.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")

    live = _eccc(
        alert_factory,
        id="K1",
        identifier="id-rev1",
        msg_type="Alert",
        expires="2099-01-01T00:00:00+00:00",
    )
    store.process(normalize_alerts([live]))
    hass.bus.async_fire.reset_mock()

    ended_rev1 = _eccc(
        alert_factory,
        id="K2",
        identifier="id-rev2",
        msg_type="Update",
        lifecycle_status="ended",
        references=("id-rev1",),
    )
    assert store.process(normalize_alerts([ended_rev1])) == []
    assert _events(hass) == ["incident_removed"]

    ended_rev2 = _eccc(
        alert_factory,
        id="K3",
        identifier="id-rev3",
        msg_type="Update",
        lifecycle_status="ended",
        references=("id-rev2",),
    )
    assert store.process(normalize_alerts([ended_rev2])) == []

    assert _events(hass) == ["incident_removed"]


def test_a_different_group_of_the_same_chain_fires_its_own_removal(hass, alert_factory):
    """One chain, two groups: ending A must not swallow the later ending of B."""
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")

    group_a_live = _eccc(
        alert_factory,
        id="A1",
        identifier="a-rev1",
        msg_type="Alert",
        expires="2099-01-01T00:00:00+00:00",
    )
    group_b_live = _eccc(
        alert_factory,
        id="B1",
        identifier="b-rev1",
        msg_type="Alert",
        expires="2099-01-01T00:00:00+00:00",
    )
    store.process(normalize_alerts([group_a_live, group_b_live]))
    hass.bus.async_fire.reset_mock()

    group_a_ended = _eccc(
        alert_factory,
        id="A2",
        identifier="a-rev2",
        msg_type="Update",
        lifecycle_status="ended",
        references=("a-rev1",),
    )
    store.process(normalize_alerts([group_a_ended]))
    assert _events(hass) == ["incident_removed"]

    group_b_ended = _eccc(
        alert_factory,
        id="B2",
        identifier="b-rev2",
        msg_type="Update",
        lifecycle_status="ended",
        references=("b-rev1",),
    )
    store.process(normalize_alerts([group_b_ended]))

    assert _events(hass) == ["incident_removed", "incident_removed"]


def test_group_revived_live_then_ended_again_fires_both_events(hass, alert_factory):
    """Revival severs the tombstoned lineage, so the next ending still fires.

    Without the severing, the later ending's ``references`` would still name
    an ancestor recorded as already-announced, and the second, genuine ending
    would be swallowed as a duplicate of the first.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts
    from custom_components.cap_alerts.store import AlertStore

    store = AlertStore(hass, "entry1", "eccc")

    ended_rev1 = _eccc(
        alert_factory,
        id="K1",
        identifier="id-rev1",
        msg_type="Update",
        lifecycle_status="ended",
    )
    store.process(normalize_alerts([ended_rev1]))
    hass.bus.async_fire.reset_mock()

    revived = _eccc(
        alert_factory,
        id="K2",
        identifier="id-rev2",
        msg_type="Update",
        expires="2099-01-01T00:00:00+00:00",
        references=("id-rev1",),
    )
    store.process(normalize_alerts([revived]))
    assert _events(hass) == ["incident_created"]

    ended_again = _eccc(
        alert_factory,
        id="K3",
        identifier="id-rev3",
        msg_type="Update",
        lifecycle_status="ended",
        references=("id-rev2",),
    )
    store.process(normalize_alerts([ended_again]))

    assert _events(hass) == ["incident_created", "incident_removed"]
