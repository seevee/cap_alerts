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


def test_parse_feature_populates_geocodes_container():
    # Every scheme NWS publishes lands in ``geocodes`` under its raw key, and
    # the container is the only place the codes are published.
    feature = _feature(["OHC049", "OHC035"])
    feature["properties"]["geocode"]["SAME"] = ["039049", "039035"]
    alert = _parse_feature(feature)
    assert alert.geocodes == {
        "UGC": ("OHC049", "OHC035"),
        "SAME": ("039049", "039035"),
    }
    attrs = alert.to_attributes()
    assert attrs["geocodes"]["UGC"] == ["OHC049", "OHC035"]
    assert attrs["geocodes"]["SAME"] == ["039049", "039035"]
    assert not [k for k in attrs if k.startswith("geocode_")]


def test_parse_feature_no_geocode_key_leaves_container_empty():
    feature = _feature([])
    del feature["properties"]["geocode"]
    alert = _parse_feature(feature)
    assert alert.geocodes == {}
    assert "geocodes" not in alert.to_attributes()


# ---------------------------------------------------------------------------
# _parse_feature envelope routing (issue #292)
# ---------------------------------------------------------------------------

# Every attribute the model used to carry for NWS alone. Their values live in
# ``parameters`` now (``VTEC``, ``NationalWeatherService``, ``SAME``) or were
# redundant with ``geocodes`` / ``references``; a consumer reads those.
_NWS_ENVELOPE_ATTRIBUTES = (
    "vtec",
    "vtec_office",
    "vtec_phenomena",
    "vtec_significance",
    "vtec_action",
    "vtec_tracking",
    "event_code_nws",
    "event_code_same",
    "affected_zones",
    "affected_zone_uris",
    "replaced_by",
    "replaced_at",
)


def _full_feature() -> dict[str, Any]:
    feature = _feature(
        ["NYZ071", "NYZ072"],
        zone_uris=[
            "https://api.weather.gov/zones/forecast/NYZ071",
            "https://api.weather.gov/zones/forecast/NYZ072",
        ],
    )
    feature["properties"].update(
        {
            "eventCode": {"SAME": ["SVR"], "NationalWeatherService": ["SVR"]},
            "parameters": {
                "AWIPSidentifier": ["SVROKX"],
                "VTEC": ["/O.NEW.KOKX.SV.W.0042.260414T1947Z-260414T2045Z/"],
            },
            "replacedBy": "https://api.weather.gov/alerts/urn:oid:next",
            "replacedAt": "2026-04-14T20:30:00-04:00",
        }
    )
    return feature


def test_parse_feature_routes_event_codes_to_parameters():
    # The GeoJSON ``eventCode`` schemes land in ``parameters`` under their own
    # names, list-valued like every other NWS parameter, next to the VTEC the
    # provider already copied there. One slot per provider, the way ECCC's
    # CAP-CP codes already ship.
    alert = _parse_feature(_full_feature())
    assert alert.parameters == {
        "SAME": ["SVR"],
        "NationalWeatherService": ["SVR"],
        "AWIPSidentifier": ["SVROKX"],
        "VTEC": ["/O.NEW.KOKX.SV.W.0042.260414T1947Z-260414T2045Z/"],
    }


def test_parse_feature_native_parameters_win_on_collision():
    # Never seen on the live feed (scripts/nws_parameters_probe.py), but the
    # rule is the feed's own parameter outranks the folded-in event code, as
    # ECCC's merge already does.
    feature = _full_feature()
    feature["properties"]["parameters"]["SAME"] = ["native"]
    alert = _parse_feature(feature)
    assert alert.parameters is not None
    assert alert.parameters["SAME"] == ["native"]
    # And the feature itself is never mutated: nothing folded either way.
    assert "NationalWeatherService" not in feature["properties"]["parameters"]
    assert "VTEC" not in feature["properties"]["eventCode"]


def test_parse_feature_without_codes_or_parameters_leaves_parameters_none():
    feature = _feature(["OHC049"])
    assert _parse_feature(feature).parameters is None


def test_parse_feature_publishes_no_nws_envelope_attributes():
    # The contract pin for issue #292: a fully populated NWS feature publishes
    # none of the one-provider names, and the model does not even carry them.
    alert = _parse_feature(_full_feature())
    attrs = alert.to_attributes()
    assert not set(_NWS_ENVELOPE_ATTRIBUTES) & set(attrs)
    for name in _NWS_ENVELOPE_ATTRIBUTES:
        assert not hasattr(alert, name)
    # What a consumer reads instead.
    assert attrs["geocodes"]["UGC"] == ["NYZ071", "NYZ072"]
    assert attrs["parameters"]["NationalWeatherService"] == ["SVR"]
