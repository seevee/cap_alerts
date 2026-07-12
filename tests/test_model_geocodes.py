"""Tests for the CAPAlert.geocodes multi-scheme container serialization."""

from __future__ import annotations

from tests.conftest import make_alert


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
