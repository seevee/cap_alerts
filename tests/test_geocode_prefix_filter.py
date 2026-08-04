"""Provider-neutral geocode-prefix filter (issue #73).

The filter is a pure list transform over ``CAPAlert.geocodes``, so it is
exercised directly rather than through a coordinator. The fail-loud contract is
the interesting part: a source that publishes *no* geocodes at all is a
capability failure (parallel to "publishes no per-alert geometry" in the GPS
filters), while zero *matches* is an ordinary quiet period.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.cap_alerts.coordinator import (
    AlertsDataUpdateCoordinator,
    filter_by_geocode_prefixes,
    matches_geocode_prefixes,
)
from custom_components.cap_alerts.model import CAPAlert, geocodes_from
from homeassistant.helpers.update_coordinator import UpdateFailed

# CMA publishes one scheme, one value per alert, and mixes code lengths within
# it — 481 of 488 sampled codes were 12 characters, 7 were 6 (Chongqing
# districts). Both widths are represented here so a future refactor cannot
# silently introduce zero-padding.
_CPEAS = "CPEAS Geographic Code"


def _alert(alert_id: str, **geocodes: tuple[str, ...]) -> CAPAlert:
    return CAPAlert(id=alert_id, geocodes=geocodes_from(dict(geocodes)))


def _cpeas(alert_id: str, *codes: str) -> CAPAlert:
    return CAPAlert(id=alert_id, geocodes=geocodes_from({_CPEAS: list(codes)}))


def _ids(alerts: list[CAPAlert]) -> list[str]:
    return [a.id for a in alerts]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_prefix_keeps_matching_and_drops_the_rest():
    alerts = [
        _cpeas("hebei", "130709000000"),
        _cpeas("beijing", "110000000000"),
    ]
    assert _ids(filter_by_geocode_prefixes(alerts, ["13"])) == ["hebei"]


def test_multiple_prefixes_union():
    alerts = [
        _cpeas("hebei", "130709000000"),
        _cpeas("beijing", "110000000000"),
        _cpeas("shandong", "370100000000"),
    ]
    kept = filter_by_geocode_prefixes(alerts, ["13", "37"])
    assert _ids(kept) == ["hebei", "shandong"]


def test_short_prefix_matches_a_short_code_of_the_same_scheme():
    # 6-character codes exist alongside 12-character ones; a scope prefix has
    # to match both without either side being padded.
    alerts = [_cpeas("chongqing", "500229"), _cpeas("chongli", "130709000000")]
    assert _ids(filter_by_geocode_prefixes(alerts, ["50"])) == ["chongqing"]


def test_full_length_code_does_not_match_a_shorter_sibling():
    # Documented limitation, locked deliberately: pasting a 12-digit code will
    # not match a 6-digit code for the same area. The field description tells
    # users to prefer the leading digits.
    alerts = [_cpeas("chongqing", "500229")]
    assert filter_by_geocode_prefixes(alerts, ["500229000000"]) == []


def test_match_is_case_insensitive_on_both_sides():
    alerts = [_alert("nws", UGC=("OHZ049",))]
    assert _ids(filter_by_geocode_prefixes(alerts, ["ohz"])) == ["nws"]
    assert _ids(filter_by_geocode_prefixes(alerts, ["OHZ"])) == ["nws"]


def test_whitespace_around_a_prefix_is_ignored():
    alerts = [_cpeas("hebei", "130709000000")]
    assert _ids(filter_by_geocode_prefixes(alerts, ["  13  "])) == ["hebei"]


def test_any_scheme_matches():
    # Scheme-agnostic by design: there is no cross-provider scheme-priority
    # registry, so a value under any valueName can satisfy the prefix.
    alerts = [_alert("multi", UGC=("OHZ049",), SAME=("039035",))]
    assert _ids(filter_by_geocode_prefixes(alerts, ["039"])) == ["multi"]


def test_matches_helper_reports_per_alert():
    alert = _cpeas("hebei", "130709000000")
    assert matches_geocode_prefixes(alert, ("13",))
    assert not matches_geocode_prefixes(alert, ("37",))


# ---------------------------------------------------------------------------
# Off / empty-input behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefixes", [[], ["   "], ["", None]])
def test_no_configured_prefixes_returns_the_list_unchanged(prefixes):
    alerts = [_cpeas("a", "130709000000"), CAPAlert(id="no-codes")]
    assert filter_by_geocode_prefixes(alerts, prefixes) is alerts


def test_empty_alert_list_returns_empty_without_raising():
    assert filter_by_geocode_prefixes([], ["13"]) == []


# ---------------------------------------------------------------------------
# Fail-loud contract
# ---------------------------------------------------------------------------


def test_no_alert_carrying_any_geocode_fails_loud():
    # Source-capability failure: prefix filtering cannot work against this feed
    # at all, so the entry should go unavailable rather than report zero alerts.
    alerts = [CAPAlert(id="a"), CAPAlert(id="b")]
    with pytest.raises(UpdateFailed, match="does not publish"):
        filter_by_geocode_prefixes(alerts, ["13"])


def test_geocodes_present_but_no_match_returns_empty_without_raising():
    # "No alerts in my area" is the normal steady state — failing here would
    # leave the entry unavailable most of the time.
    alerts = [_cpeas("beijing", "110000000000")]
    assert filter_by_geocode_prefixes(alerts, ["13"]) == []


def test_partial_geocode_coverage_does_not_fail_loud():
    # Only one alert needs codes for the filter to be viable; the code-less
    # ones are simply dropped, as they cannot match any prefix.
    alerts = [CAPAlert(id="bare"), _cpeas("hebei", "130709000000")]
    assert _ids(filter_by_geocode_prefixes(alerts, ["13"])) == ["hebei"]


# ---------------------------------------------------------------------------
# One-shot no-match warning
# ---------------------------------------------------------------------------


def _bare_coordinator():
    """A coordinator with only the fields the warning path touches.

    ``__new__`` skips ``__init__`` so this needs no hass, entry, or provider
    wiring — the method under test reads two attributes and the logger.
    """
    coord = AlertsDataUpdateCoordinator.__new__(AlertsDataUpdateCoordinator)
    coord._geocode_no_match_warned = False
    coord._provider = SimpleNamespace(name="wmo")
    return coord


def test_no_match_warns_once_per_streak(caplog):
    coord = _bare_coordinator()
    alerts = [_cpeas("beijing", "110000000000")]

    with caplog.at_level(logging.WARNING):
        coord._warn_geocode_no_match(alerts, [], ["13"])
        coord._warn_geocode_no_match(alerts, [], ["13"])

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # Actionable: names the prefix that missed and a code actually published.
    assert "13" in message
    assert "110000000000" in message


def test_warning_rearms_after_a_match(caplog):
    coord = _bare_coordinator()
    alerts = [_cpeas("beijing", "110000000000")]

    with caplog.at_level(logging.WARNING):
        coord._warn_geocode_no_match(alerts, [], ["13"])
        coord._warn_geocode_no_match(alerts, alerts, ["13"])  # match: resets
        coord._warn_geocode_no_match(alerts, [], ["13"])

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_an_empty_feed_does_not_warn(caplog):
    """Nothing was filtered out — there is no prefix problem to report."""
    coord = _bare_coordinator()
    with caplog.at_level(logging.WARNING):
        coord._warn_geocode_no_match([], [], ["13"])
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
