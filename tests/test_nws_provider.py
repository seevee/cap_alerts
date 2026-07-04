"""Tests for the NWS provider — marine classification via UGC/zone prefixes."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load(name: str) -> types.ModuleType:
    full = f"cap_alerts.{name}"
    if full in sys.modules:
        return sys.modules[full]
    pkg = sys.modules.get("cap_alerts")
    if pkg is None:
        pkg = types.ModuleType("cap_alerts")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = pkg
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_provider(name: str) -> types.ModuleType:
    full = f"cap_alerts.providers.{name}"
    if full in sys.modules:
        return sys.modules[full]
    pkg_key = "cap_alerts.providers"
    if pkg_key not in sys.modules:
        providers_pkg = types.ModuleType(pkg_key)
        providers_pkg.__path__ = [str(_PKG_DIR / "providers")]
        sys.modules[pkg_key] = providers_pkg
    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
_load("model")
_nws_mod = _load_provider("nws")

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
