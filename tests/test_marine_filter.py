"""Tests for the provider-neutral marine filter and is_marine attribute surface."""

from __future__ import annotations

from tests.conftest import make_alert
from tests.test_coordinator_tracker import _load_coordinator

coordinator = _load_coordinator()
exclude_marine_alerts = coordinator.exclude_marine_alerts


# ---------------------------------------------------------------------------
# exclude_marine_alerts
# ---------------------------------------------------------------------------


def test_exclude_marine_noop_when_disabled():
    alerts = [
        make_alert(id="land", is_marine=False),
        make_alert(id="sea", is_marine=True),
    ]
    result = exclude_marine_alerts(alerts, enabled=False)
    assert result is alerts  # unchanged, same list object


def test_exclude_marine_drops_marine_when_enabled():
    alerts = [
        make_alert(id="land", is_marine=False),
        make_alert(id="sea", is_marine=True),
    ]
    result = exclude_marine_alerts(alerts, enabled=True)
    assert [a.id for a in result] == ["land"]


def test_exclude_marine_keeps_all_when_none_marine():
    alerts = [make_alert(id="a"), make_alert(id="b")]
    result = exclude_marine_alerts(alerts, enabled=True)
    assert [a.id for a in result] == ["a", "b"]


def test_exclude_marine_empty_list():
    assert exclude_marine_alerts([], enabled=True) == []


# ---------------------------------------------------------------------------
# is_marine attribute surfacing
# ---------------------------------------------------------------------------


def test_to_attributes_omits_is_marine_when_false():
    alert = make_alert(is_marine=False)
    assert "is_marine" not in alert.to_attributes()


def test_to_attributes_surfaces_is_marine_when_true():
    alert = make_alert(is_marine=True)
    assert alert.to_attributes()["is_marine"] is True
