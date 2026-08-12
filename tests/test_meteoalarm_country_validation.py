"""MeteoAlarm country-code validator."""

from __future__ import annotations

import pytest

from custom_components.cap_alerts.flows.meteoalarm import _validate_country
from custom_components.cap_alerts.const import (
    METEOALARM_COUNTRIES,
    METEOALARM_COUNTRY_NAMES,
    METEOALARM_COUNTRY_SLUGS,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("de", "DE"),
        ("DE", "DE"),
        (" fr ", "FR"),
        ("uk", "UK"),
        ("CH", "CH"),
    ],
)
def test_valid_country_codes_are_uppercased(raw, expected):
    cleaned, err = _validate_country(raw)
    assert err is None
    assert cleaned == expected


@pytest.mark.parametrize("raw", ["XX", "USA", "us", "ca", "mx"])
def test_unknown_country_is_invalid(raw):
    _, err = _validate_country(raw)
    assert err == "invalid_country"


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_empty_or_whitespace_is_invalid(raw):
    _, err = _validate_country(raw)
    assert err == "invalid_country"


def test_country_set_is_immutable_and_nonempty():
    assert isinstance(METEOALARM_COUNTRIES, frozenset)
    assert len(METEOALARM_COUNTRIES) >= 30
    # Spot-check a few representative codes.
    for code in ("DE", "FR", "IT", "ES", "PL"):
        assert code in METEOALARM_COUNTRIES


def test_country_names_match_slugs():
    assert set(METEOALARM_COUNTRY_NAMES) == set(METEOALARM_COUNTRY_SLUGS)
    for code, label in METEOALARM_COUNTRY_NAMES.items():
        assert isinstance(label, str)
        assert label.strip(), f"empty label for {code}"
