"""Tests for the CAPAlert.geocodes multi-scheme container serialization."""

from __future__ import annotations

import sys

import pytest

from tests.conftest import CAPAlert, make_alert

# conftest loads the model as ``cap_alerts.model``, a distinct module object from
# ``custom_components.cap_alerts.model`` when the HA plugin is active. Source the
# helpers from whichever copy defines the CAPAlert under test so they operate on
# the same container the properties read.
_model = sys.modules[CAPAlert.__module__]
canonical_scheme = _model.canonical_scheme
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
# Canonical scheme names — versioned valueNames publish under a stable key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("layer:EC-MSC-SMC:1.0:CLC", "CLC"),
        ("layer:EC-MSC-SMC:1.1:CLC", "CLC"),
        # A bump the integration has never seen still lands on the same key,
        # which an enumerated accept-list could not do.
        ("layer:EC-MSC-SMC:2.4.1:CLC", "CLC"),
        ("profile:CAP-CP:Location:0.3", "SGC"),
        ("profile:CAP-CP:Location:1.0", "SGC"),
        # Already canonical, and schemes with no rule, pass through untouched.
        ("UGC", "UGC"),
        ("SAME", "SAME"),
        ("EMMA_ID", "EMMA_ID"),
        ("NUTS3", "NUTS3"),
    ],
)
def test_canonical_scheme(raw, expected):
    assert canonical_scheme(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # Anchored on both ends: a scheme that merely contains a known name is
        # a different scheme and keeps its own key.
        "layer:EC-MSC-SMC:1.0:CLC:SUB",
        "x-layer:EC-MSC-SMC:1.0:CLC",
        "layer:EC-MSC-SMC:CLC",
        "profile:CAP-CP:Location",
        "profile:CAP-CP:Location:0.3:extra",
    ],
)
def test_canonical_scheme_leaves_near_misses_alone(raw):
    assert canonical_scheme(raw) == raw


def test_geocodes_from_canonicalizes_versioned_schemes():
    geocodes = geocodes_from({"layer:EC-MSC-SMC:1.0:CLC": ["071100"]})
    assert geocodes == {"CLC": ("071100",)}
    assert "layer:EC-MSC-SMC:1.0:CLC" not in geocodes


def test_geocodes_from_unions_schemes_that_canonicalize_together():
    # A feed mid-version-bump publishes both. Every code survives, once, in
    # first-seen order, rather than one version silently winning.
    geocodes = geocodes_from(
        {
            "layer:EC-MSC-SMC:1.0:CLC": ["071100", "090000"],
            "layer:EC-MSC-SMC:1.1:CLC": ["090000", "099999"],
        }
    )
    assert geocodes == {"CLC": ("071100", "090000", "099999")}


def test_container_reads_a_version_the_code_has_never_seen():
    alert = make_alert(geocodes=geocodes_from({"layer:EC-MSC-SMC:9.9:CLC": ["071100"]}))
    assert alert.geocodes["CLC"] == ("071100",)


# ---------------------------------------------------------------------------
# The attribute surface carries the container, and only the container


def test_to_attributes_publishes_the_container_once():
    # The ``geocode_*`` aliases republished the same codes a second time
    # (issue #150) and are gone from the model altogether (issue #292): the
    # container is the one read path, in code and on the wire.
    alert = make_alert(geocodes=geocodes_from({"UGC": ["OHC049"]}))
    attrs = alert.to_attributes()
    assert attrs["geocodes"] == {"UGC": ["OHC049"]}
    assert not [k for k in attrs if k.startswith("geocode_")]
    assert not hasattr(alert, "geocode_ugc")


def test_to_attributes_omits_container_and_aliases_when_no_geocodes():
    attrs = make_alert().to_attributes()
    assert "geocodes" not in attrs
    assert not [k for k in attrs if k.startswith("geocode_")]
