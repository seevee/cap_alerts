"""MeteoAlarm JSON warnings feed parsing."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from custom_components.cap_alerts.normalize import normalize_alerts

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _ensure_update_coordinator_stub() -> None:
    """Provide ``UpdateFailed`` even when other tests stubbed HA submodules.

    ``test_geometry_endpoint`` replaces ``homeassistant.helpers`` with a bare
    module which removes ``update_coordinator``. When pytest collects tests
    in that order, the meteoalarm provider import fails. Inject a minimal
    stub so the provider loads either way.
    """
    helpers = sys.modules.get("homeassistant.helpers")
    if helpers is None:
        return
    if hasattr(helpers, "update_coordinator"):
        return
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        """Test stub of homeassistant.helpers.update_coordinator.UpdateFailed."""

    class CoordinatorEntity:
        """Test stub of homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    CoordinatorEntity.__class_getitem__ = classmethod(  # type: ignore[attr-defined]
        lambda cls, _i: cls
    )
    uc.UpdateFailed = UpdateFailed
    uc.CoordinatorEntity = CoordinatorEntity
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc  # type: ignore[attr-defined]


def _load_meteoalarm():
    """Load the MeteoAlarm provider module by file path.

    The integration's package ``__init__`` imports HA platforms; tests bypass
    that by loading the module file directly. ``model`` and ``const`` are
    preloaded so the relative imports inside the provider resolve.
    """
    full = "cap_alerts.providers.meteoalarm"
    if full in sys.modules:
        return sys.modules[full]

    if "cap_alerts" not in sys.modules:
        parent = types.ModuleType("cap_alerts")
        parent.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = parent

    if "cap_alerts.const" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "cap_alerts.const", _PKG_DIR / "const.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cap_alerts.const"] = mod
        spec.loader.exec_module(mod)

    if "cap_alerts.providers" not in sys.modules:
        providers_pkg = types.ModuleType("cap_alerts.providers")
        providers_pkg.__path__ = [str(_PKG_DIR / "providers")]
        sys.modules["cap_alerts.providers"] = providers_pkg

    _ensure_update_coordinator_stub()

    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / "meteoalarm.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


meteoalarm = _load_meteoalarm()


@pytest.fixture
def feed_de() -> dict:
    return json.loads((_FIXTURE_DIR / "meteoalarm_de.json").read_text(encoding="utf-8"))


def _parse(feed: dict, preferred_prefix: str = "de"):
    """Run the provider's per-warning conversion across a JSON payload."""
    alerts = []
    for warning in feed["warnings"]:
        alert = meteoalarm._warning_to_alert(warning, preferred_prefix)
        if alert is not None:
            alerts.append(alert)
    return alerts


def test_parses_three_active_warnings(feed_de):
    alerts = _parse(feed_de)
    # Fixture has 3 actual warnings plus one Test status that is filtered.
    assert len(alerts) == 3
    for a in alerts:
        assert a.provider == "meteoalarm"


def test_identifier_hashing_stable(feed_de):
    a1 = _parse(feed_de)
    a2 = _parse(feed_de)
    assert [a.id for a in a1] == [a.id for a in a2]
    for a in a1:
        assert len(a.id) == 12
        assert all(c in "0123456789abcdef" for c in a.id)


def test_severity_passthrough_for_normalization(feed_de):
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.severity == "Moderate"
    (out,) = normalize_alerts([gusts])
    assert out.severity_normalized == "moderate"


def test_language_merge_de_primary_en_alt(feed_de):
    alerts = _parse(feed_de, preferred_prefix="de")
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.language.startswith("de")
    assert gusts.headline.startswith("Amtliche")
    assert gusts.headline_alt
    assert gusts.language_alt.startswith("en")
    assert "GALE-FORCE" in gusts.headline_alt.upper()


def test_language_merge_en_primary_when_preferred_missing(feed_de):
    alerts = _parse(feed_de, preferred_prefix="fr")
    gusts = next(a for a in alerts if a.event == "gale-force gusts")
    # No fr in fixture; falls back to en (the generic English fallback rule).
    assert gusts.language.startswith("en")


def test_no_geometry_from_json_feed(feed_de):
    # MeteoAlarm warnings never carry polygons in the JSON feed.
    for a in _parse(feed_de):
        assert a.geometry is None


def test_emma_geocodes_collected(feed_de):
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.geocode_same
    for code in gusts.geocode_same:
        assert code.startswith("DE")


def test_awareness_level_in_parameters(feed_de):
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.parameters is not None
    assert gusts.parameters.get("awareness_level", "").startswith("3;")


def test_test_status_warning_filtered(feed_de):
    # Fixture includes a status="Test" warning; ensure it's not in the output.
    events = {a.event for a in _parse(feed_de)}
    assert "Test Hazard" not in events


def test_area_desc_joined_across_areas(feed_de):
    alerts = _parse(feed_de)
    # The fixture trims each warning to two areas; joined string contains both.
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert "," in gusts.area_desc or len(gusts.geocode_same) == 1


def test_repeated_parameters_joined(feed_de):
    # The DE fixture's gusts warning has two ``impacts`` parameter entries —
    # the provider joins repeats with "; " rather than dropping them.
    alerts = _parse(feed_de)
    gusts = next(a for a in alerts if a.event == "STURMBÖEN")
    assert gusts.parameters is not None
    impacts = gusts.parameters.get("impacts", "")
    assert ";" in impacts
