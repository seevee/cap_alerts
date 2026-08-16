"""Tests for the CAPAlert.geocodes multi-scheme container serialization."""

from __future__ import annotations

import sys
from types import MappingProxyType

import pytest

from tests.conftest import CAPAlert, make_alert

# conftest loads the model as ``cap_alerts.model``, a distinct module object from
# ``custom_components.cap_alerts.model`` when the HA plugin is active. Source the
# helpers from whichever copy defines the CAPAlert under test so monkeypatching
# the registry actually reaches the properties.
_model = sys.modules[CAPAlert.__module__]
GEOCODE_SCHEME_ALIASES = _model.GEOCODE_SCHEME_ALIASES
geocodes_from = _model.geocodes_from


def test_to_attributes_serializes_geocodes():
    alert = make_alert(geocodes={"NUTS3": ("FR614", "FR611")})
    attrs = alert.to_attributes()
    assert attrs["geocodes"] == {"NUTS3": ["FR614", "FR611"]}


def test_to_attributes_omits_empty_geocodes():
    alert = make_alert()
    assert "geocodes" not in alert.to_attributes()


def test_to_attributes_serializes_multi_scheme_geocodes():
    alert = make_alert(geocodes={"EMMA_ID": ("DE343",), "WARNCELLID": ("114521000",)})
    attrs = alert.to_attributes()
    assert attrs["geocodes"] == {"EMMA_ID": ["DE343"], "WARNCELLID": ["114521000"]}


# ---------------------------------------------------------------------------
# geocodes_from — the single normalization funnel


def test_geocodes_from_dedupes_order_preserving():
    assert geocodes_from({"UGC": ["A", "A", "B", "A"]}) == {"UGC": ("A", "B")}


def test_geocodes_from_drops_empty_schemes_and_values():
    # Empty scheme key, empty value list, and empty individual values all drop.
    assert geocodes_from({"": ["A"]}) == {}
    assert geocodes_from({"X": []}) == {}
    assert geocodes_from({"X": ["", ""]}) == {}
    assert geocodes_from({"X": ["", "A"]}) == {"X": ("A",)}


def test_geocodes_from_empty_returns_shared_singleton():
    # Same object the dataclass default uses, so an empty container allocates
    # nothing per alert.
    assert geocodes_from({}) is geocodes_from({"X": []})


def test_geocodes_from_result_is_immutable():
    geocodes = geocodes_from({"UGC": ["OHC049"]})
    with pytest.raises(TypeError):
        geocodes["UGC"] = ("nope",)  # type: ignore[index]


# ---------------------------------------------------------------------------
# Promoted aliases — derived properties, never stored


@pytest.mark.parametrize(
    ("alias", "scheme"),
    [
        ("geocode_ugc", "UGC"),
        ("geocode_same", "SAME"),
        ("geocode_clc", "layer:EC-MSC-SMC:1.0:CLC"),
        ("geocode_sgc", "profile:CAP-CP:Location:0.3"),
    ],
)
def test_alias_property_reads_its_scheme(alias, scheme):
    alert = make_alert(geocodes=geocodes_from({scheme: ["001", "002"]}))
    assert getattr(alert, alias) == ("001", "002")


@pytest.mark.parametrize("alias", list(GEOCODE_SCHEME_ALIASES))
def test_alias_property_empty_when_scheme_absent(alias):
    alert = make_alert(geocodes=geocodes_from({"NUTS3": ["FR614"]}))
    assert getattr(alert, alias) == ()


def test_alias_accept_list_first_match_wins(monkeypatch):
    # The accept-list absorbs a source bumping its scheme version; ordering is
    # deterministic — the first listed valueName with a non-empty value wins,
    # regardless of the container's own key order.
    monkeypatch.setattr(
        _model,
        "GEOCODE_SCHEME_ALIASES",
        MappingProxyType(
            {"geocode_clc": ("layer:EC-MSC-SMC:1.1:CLC", "layer:EC-MSC-SMC:1.0:CLC")}
        ),
    )
    both = make_alert(
        geocodes=geocodes_from(
            {
                "layer:EC-MSC-SMC:1.0:CLC": ["071100"],
                "layer:EC-MSC-SMC:1.1:CLC": ["999999"],
            }
        )
    )
    assert both.geocode_clc == ("999999",)
    # Falls through to the second entry when the preferred scheme is absent.
    older = make_alert(geocodes=geocodes_from({"layer:EC-MSC-SMC:1.0:CLC": ["071100"]}))
    assert older.geocode_clc == ("071100",)


def test_geocode_scheme_aliases_is_immutable():
    with pytest.raises(TypeError):
        GEOCODE_SCHEME_ALIASES["geocode_new"] = ("X",)  # type: ignore[index]


# ---------------------------------------------------------------------------
# The attribute surface carries the container, never the aliases


def test_to_attributes_publishes_the_container_not_the_alias():
    # The alias is a read path in code; republishing it would put the same
    # codes on the wire twice (issue #150).
    alert = make_alert(geocodes=geocodes_from({"UGC": ["OHC049"]}))
    attrs = alert.to_attributes()
    assert attrs["geocodes"] == {"UGC": ["OHC049"]}
    assert alert.geocode_ugc == ("OHC049",)
    assert "geocode_ugc" not in attrs


def test_to_attributes_omits_alias_for_unpromoted_scheme():
    alert = make_alert(geocodes=geocodes_from({"NUTS3": ["FR614"]}))
    attrs = alert.to_attributes()
    assert attrs["geocodes"] == {"NUTS3": ["FR614"]}
    assert not [k for k in attrs if k.startswith("geocode_")]


def test_to_attributes_omits_container_and_aliases_when_no_geocodes():
    attrs = make_alert().to_attributes()
    assert "geocodes" not in attrs
    assert not [k for k in attrs if k.startswith("geocode_")]
