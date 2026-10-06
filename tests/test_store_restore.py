"""A restored alert takes the steady-state path, not the registry one (issue #281).

The coordinator persists the live set and seeds the store from it at boot, so
the first reconciliation after a restart diffs against what the alerts actually
said. Ids with no stored record keep the #250 registry path, pinned in
``test_store_restart.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.cap_alerts.const import (
    EVENT_INCIDENT_CREATED,
    EVENT_INCIDENT_REMOVED,
    EVENT_INCIDENT_UPDATED,
)
from custom_components.cap_alerts.store import AlertStore

CONFIRMED_AT = "2026-10-05T12:00:00+00:00"
BOOT = datetime(2026, 10, 6, tzinfo=timezone.utc)
PAST = "2026-10-05T18:00:00+00:00"
FUTURE = "2026-10-08T00:00:00+00:00"


@pytest.fixture
def hass():
    h = MagicMock()
    h.bus.async_fire = MagicMock()
    h.entity_registry.async_get_entity_id.return_value = None
    return h


@pytest.fixture
def registry(monkeypatch, hass):
    """Registry entries for this entry; tests append unique_ids before building."""
    entries: list[SimpleNamespace] = []
    monkeypatch.setattr(
        "custom_components.cap_alerts.store.er.async_get",
        lambda h: hass.entity_registry,
    )
    monkeypatch.setattr(
        "custom_components.cap_alerts.store.er.async_entries_for_config_entry",
        lambda reg, entry_id: entries,
    )
    return entries


def _known(registry, *alert_ids: str) -> None:
    """Registry rows as HA keeps them: unique_id plus the name at registration."""
    registry.append(SimpleNamespace(unique_id="entry1_count", original_name="Count"))
    registry.extend(
        SimpleNamespace(
            unique_id=f"entry1_nws_{alert_id}", original_name=f"Event {alert_id}"
        )
        for alert_id in alert_ids
    )


def _fired(hass) -> list[tuple[str, str]]:
    return [
        (call.args[0], call.args[1]["incident_id"])
        for call in hass.bus.async_fire.call_args_list
    ]


def _payloads(hass, event_type: str) -> list[dict]:
    return [
        call.args[1]
        for call in hass.bus.async_fire.call_args_list
        if call.args[0] == event_type
    ]


def _at(moment: datetime):
    """Pin the store's clock. Subclassed so ``fromisoformat`` keeps working."""

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    return patch("custom_components.cap_alerts.store.datetime", _Pinned)


def test_restore_fires_nothing_by_itself(hass, registry, alert_factory):
    """A failed setup rebuilds the store on every retry, so restore must be silent."""
    store = AlertStore(hass, "entry1", "nws")

    for _ in range(3):
        store.restore(
            [
                alert_factory(id="live", phase="update", expires=FUTURE),
                alert_factory(id="old", phase="update", expires=PAST),
            ],
            confirmed_at=CONFIRMED_AT,
            now=BOOT,
        )

    assert _fired(hass) == []


def test_restored_live_alert_is_revalidated_silently(hass, registry, alert_factory):
    store = AlertStore(hass, "entry1", "nws")
    saved = alert_factory(id="a", phase="update")
    seeded = store.restore([saved], confirmed_at=CONFIRMED_AT)
    assert seeded[0].stale is True
    assert seeded[0].last_confirmed == CONFIRMED_AT

    result = store.process([alert_factory(id="a", phase="update")])

    assert [a.id for a in result] == ["a"]
    assert result[0].stale is False
    assert result[0].phase_changed is False
    assert _fired(hass) == []


def test_restored_alert_with_changed_content_fires_updated(
    hass, registry, alert_factory
):
    store = AlertStore(hass, "entry1", "nws")
    store.restore(
        [alert_factory(id="a", phase="update", headline="old")],
        confirmed_at=CONFIRMED_AT,
    )

    store.process([alert_factory(id="a", phase="update", headline="new")])

    assert _fired(hass) == [(EVENT_INCIDENT_UPDATED, "a")]
    assert _payloads(hass, EVENT_INCIDENT_UPDATED)[0]["changed_fields"] == ["headline"]


def test_restored_alert_past_expires_is_removed_on_the_first_process_with_content(
    hass, registry, alert_factory
):
    store = AlertStore(hass, "entry1", "nws")
    seeded = store.restore(
        [
            alert_factory(
                id="a",
                phase="update",
                expires=PAST,
                area_desc="Saved County",
                event="Frost Advisory",
            )
        ],
        confirmed_at=CONFIRMED_AT,
        now=BOOT,
    )
    # Seeded like any other: the first process() finds it absent and expired.
    assert [a.id for a in seeded] == ["a"]
    assert _fired(hass) == []

    result = store.process([])

    assert result == []
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]
    payload = _payloads(hass, EVENT_INCIDENT_REMOVED)[0]
    assert payload["phase"] == "expired"
    assert payload["phase_changed"] is True
    assert payload["area_desc"] == "Saved County"
    assert payload["event"] == "Frost Advisory"
    assert payload["incident_id"] == "a"


def test_expired_at_boot_is_not_fired_twice_when_upstream_still_publishes_it(
    hass, registry, alert_factory
):
    store = AlertStore(hass, "entry1", "nws")
    store.restore(
        [alert_factory(id="a", phase="update", expires=PAST)],
        confirmed_at=CONFIRMED_AT,
        now=BOOT,
    )

    for _ in range(3):
        result = store.process([alert_factory(id="a", phase="expired", expires=PAST)])

    assert result == []
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]


def test_expired_at_boot_that_arrives_live_is_an_update_not_a_removal(
    hass, registry, alert_factory
):
    """Upstream extended the expiry while HA was down."""
    store = AlertStore(hass, "entry1", "nws")
    store.restore(
        [alert_factory(id="a", phase="update", expires=PAST)],
        confirmed_at=CONFIRMED_AT,
        now=BOOT,
    )

    result = store.process(
        [alert_factory(id="a", phase="update", expires="2099-01-01T00:00:00+00:00")]
    )

    assert [a.id for a in result] == ["a"]
    assert _fired(hass) == [(EVENT_INCIDENT_UPDATED, "a")]
    assert "expires" in _payloads(hass, EVENT_INCIDENT_UPDATED)[0]["changed_fields"]


def test_restored_absent_alert_is_retained_stale_to_its_expiry(
    hass, registry, alert_factory
):
    """The boot absence-rule fix: a feed gap at boot no longer reads as an ending."""
    store = AlertStore(hass, "entry1", "nws")
    store.restore(
        [alert_factory(id="a", phase="update", expires=FUTURE)],
        confirmed_at=CONFIRMED_AT,
        now=BOOT,
    )

    with _at(BOOT):
        for _ in range(3):
            result = store.process([])

    assert [a.id for a in result] == ["a"]
    assert result[0].stale is True
    assert result[0].last_confirmed == CONFIRMED_AT
    assert _fired(hass) == []

    with _at(datetime(2026, 10, 9, tzinfo=timezone.utc)):
        result = store.process([])

    assert result == []
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]
    assert _payloads(hass, EVENT_INCIDENT_REMOVED)[0]["phase"] == "expired"


def test_restored_absent_alert_on_an_absence_ends_source_is_removed_with_content(
    hass, registry, alert_factory
):
    """AU blanks ``expires`` and has no terminal vocabulary, so absence ends it."""
    store = AlertStore(hass, "entry1", "au")
    store.restore(
        [
            alert_factory(
                id="a", provider="au", phase="update", expires="", area_desc="Teelah"
            )
        ],
        confirmed_at=CONFIRMED_AT,
    )

    result = store.process([])

    assert result == []
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]
    payload = _payloads(hass, EVENT_INCIDENT_REMOVED)[0]
    assert payload["phase"] == "cancel"
    assert payload["area_desc"] == "Teelah"


def test_restored_alert_terminal_upstream_carries_removal_reason(
    hass, registry, alert_factory
):
    """Terminal on the boot fetch is suppressed for unknown ids, not restored ones."""
    store = AlertStore(hass, "entry1", "eccc")
    store.restore(
        [alert_factory(id="a", provider="eccc", phase="update")],
        confirmed_at=CONFIRMED_AT,
    )

    store.process(
        [
            alert_factory(
                id="a", provider="eccc", phase="cancel", lifecycle_status="ended"
            )
        ]
    )

    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]
    assert _payloads(hass, EVENT_INCIDENT_REMOVED)[0]["removal_reason"] == "ended"


def test_restored_id_leaves_boot_pending(hass, registry, alert_factory):
    _known(registry, "a")
    store = AlertStore(hass, "entry1", "nws")
    assert store.boot_pending == {"a"}

    store.restore([alert_factory(id="a", phase="update")], confirmed_at=CONFIRMED_AT)

    assert store.boot_pending == frozenset()


def test_registry_only_ids_still_take_the_two_fetch_path(hass, registry, alert_factory):
    _known(registry, "a", "b")
    store = AlertStore(hass, "entry1", "nws")
    store.restore([alert_factory(id="a", phase="update")], confirmed_at=CONFIRMED_AT)

    store.process([])
    assert _fired(hass) == []

    result = store.process([])

    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "b")]
    assert _payloads(hass, EVENT_INCIDENT_REMOVED)[0]["area_desc"] == ""
    assert [a.id for a in result] == ["a"]
    assert result[0].stale is True


def test_already_stale_alert_keeps_its_own_last_confirmed(
    hass, registry, alert_factory
):
    store = AlertStore(hass, "entry1", "nws")
    seeded = store.restore(
        [
            alert_factory(
                id="a",
                phase="update",
                stale=True,
                last_confirmed="2026-01-01T00:00:00+00:00",
            )
        ],
        confirmed_at=CONFIRMED_AT,
    )

    assert seeded[0].last_confirmed == "2026-01-01T00:00:00+00:00"
    result = store.process([])
    assert result[0].last_confirmed == "2026-01-01T00:00:00+00:00"


def test_restored_alert_absent_after_a_scope_change_is_removed(
    hass, registry, alert_factory
):
    """Moved out of its area while HA was down: out of scope, not unobserved."""
    store = AlertStore(hass, "entry1", "nws")
    store.restore(
        [alert_factory(id="a", phase="update", area_desc="Old Town")],
        confirmed_at=CONFIRMED_AT,
    )

    result = store.process([], scope_changed=True)

    assert result == []
    assert _fired(hass) == [(EVENT_INCIDENT_REMOVED, "a")]
    assert _payloads(hass, EVENT_INCIDENT_REMOVED)[0]["area_desc"] == "Old Town"


def test_restored_and_expired_counts_are_exposed(hass, registry, alert_factory):
    store = AlertStore(hass, "entry1", "nws")
    assert (store.restored_at_boot, store.expired_at_boot) == (0, 0)

    store.restore(
        [
            alert_factory(id="a", phase="update", expires=FUTURE),
            alert_factory(id="b", phase="update", expires=FUTURE),
            alert_factory(id="c", phase="update", expires=PAST),
        ],
        confirmed_at=CONFIRMED_AT,
        now=BOOT,
    )
    store.process([], fetched=False)

    assert (store.restored_at_boot, store.expired_at_boot) == (3, 1)
    assert not any(event == EVENT_INCIDENT_CREATED for event, _ in _fired(hass))
