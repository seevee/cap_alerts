"""MeteoAlarm country-code validator."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load_const():
    full = "cap_alerts.const"
    if full in sys.modules:
        return sys.modules[full]
    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / "const.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Load const so the validator can import METEOALARM_COUNTRIES via the
# `from .const import …` line in config_flow.py.
_load_const()


def _validate_country(value: str):
    """Reproduce config_flow._validate_country without importing voluptuous/HA.

    The config-flow module pulls in `homeassistant.config_entries` which we
    don't want as a test dep; the validator itself is a small pure function
    we can test against the same const dataset.
    """
    from cap_alerts.const import METEOALARM_COUNTRIES

    cleaned = value.strip().upper()
    if not cleaned or cleaned not in METEOALARM_COUNTRIES:
        return value, "invalid_country"
    return cleaned, None


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
    from cap_alerts.const import METEOALARM_COUNTRIES

    assert isinstance(METEOALARM_COUNTRIES, frozenset)
    assert len(METEOALARM_COUNTRIES) >= 30
    # Spot-check a few representative codes.
    for code in ("DE", "FR", "IT", "ES", "PL"):
        assert code in METEOALARM_COUNTRIES
