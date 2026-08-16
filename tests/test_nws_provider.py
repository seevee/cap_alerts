"""Tests for the NWS provider — marine classification via UGC/zone prefixes."""

from __future__ import annotations

from typing import Any

from custom_components.cap_alerts.providers import nws as _nws_mod


_is_marine_nws = _nws_mod._is_marine_nws
_parse_feature = _nws_mod._parse_feature
NWS_MARINE_UGC_PREFIXES = _nws_mod.NWS_MARINE_UGC_PREFIXES


# ---------------------------------------------------------------------------
# _is_marine_nws unit tests
# ---------------------------------------------------------------------------


def test_is_marine_nws_true_for_marine_prefixes():
    assert _is_marine_nws(("ANZ450",)) is True
    assert _is_marine_nws(("GMZ650",)) is True
    assert _is_marine_nws(("LEZ444",)) is True


def test_is_marine_nws_true_when_any_code_is_marine():
    # A mixed area (land + marine zone) still classifies as marine.
    assert _is_marine_nws(("OHC049", "ANZ450")) is True


def test_is_marine_nws_false_for_land_codes():
    assert _is_marine_nws(("OHC049", "NYZ072")) is False


def test_is_marine_nws_false_for_empty():
    assert _is_marine_nws(()) is False


def test_marine_prefixes_disjoint_from_common_state_codes():
    # Sanity: state postal codes used as UGC prefixes never collide with the
    # marine-area set, so a prefix test can't hide a land alert.
    for state in ("OH", "NY", "CA", "TX", "FL", "AK", "HI"):
        assert state not in NWS_MARINE_UGC_PREFIXES


# ---------------------------------------------------------------------------
# _parse_feature marine wiring tests
# ---------------------------------------------------------------------------


def _feature(ugc: list[str], zone_uris: list[str] | None = None) -> dict[str, Any]:
    return {
        "geometry": None,
        "properties": {
            "id": "https://api.weather.gov/alerts/urn:oid:test",
            "event": "Test Warning",
            "affectedZones": zone_uris or [],
            "geocode": {"UGC": ugc},
        },
    }


def test_parse_feature_sets_is_marine_for_marine_ugc():
    alert = _parse_feature(_feature(["ANZ450"]))
    assert alert.is_marine is True


def test_parse_feature_land_ugc_not_marine():
    alert = _parse_feature(_feature(["OHC049"]))
    assert alert.is_marine is False


def test_parse_feature_marine_from_zone_uri_only():
    # No UGC geocode, but the affectedZones URI resolves to a marine zone code.
    feature = _feature([], zone_uris=["https://api.weather.gov/zones/forecast/GMZ650"])
    alert = _parse_feature(feature)
    assert alert.is_marine is True


def test_parse_feature_no_geocodes_not_marine():
    alert = _parse_feature(_feature([]))
    assert alert.is_marine is False


# ---------------------------------------------------------------------------
# _parse_feature geocode container tests
# ---------------------------------------------------------------------------


def test_parse_feature_populates_geocodes_container_and_aliases():
    # Every scheme NWS publishes lands in ``geocodes`` under its raw key; UGC
    # and SAME are additionally reachable through their accessors, which the
    # attributes deliberately do not republish.
    feature = _feature(["OHC049", "OHC035"])
    feature["properties"]["geocode"]["SAME"] = ["039049", "039035"]
    alert = _parse_feature(feature)
    assert alert.geocodes == {
        "UGC": ("OHC049", "OHC035"),
        "SAME": ("039049", "039035"),
    }
    assert alert.geocode_ugc == ("OHC049", "OHC035")
    assert alert.geocode_same == ("039049", "039035")
    attrs = alert.to_attributes()
    assert attrs["geocodes"]["UGC"] == ["OHC049", "OHC035"]
    assert attrs["geocodes"]["SAME"] == ["039049", "039035"]
    assert not [k for k in attrs if k.startswith("geocode_")]


def test_parse_feature_no_geocode_key_leaves_container_empty():
    feature = _feature([])
    del feature["properties"]["geocode"]
    alert = _parse_feature(feature)
    assert alert.geocodes == {}
    assert alert.geocode_ugc == ()
    assert "geocodes" not in alert.to_attributes()
