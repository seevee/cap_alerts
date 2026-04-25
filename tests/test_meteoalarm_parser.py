"""MeteoAlarm CAP Atom feed parsing."""

from __future__ import annotations

import importlib.util
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
def feed_de_xml() -> str:
    return (_FIXTURE_DIR / "meteoalarm_de.xml").read_text(encoding="utf-8")


def _parse(xml_text: str, preferred_prefix: str = "de"):
    """Parse a feed string into CAPAlert objects via the provider's helpers.

    Mirrors ``MeteoAlarmProvider.async_fetch`` without the HTTP layer.
    """
    from defusedxml import ElementTree as ET

    root = ET.fromstring(xml_text)
    alerts = []
    NS_ATOM = meteoalarm.NS_ATOM
    NS_CAP = meteoalarm.NS_CAP
    for entry in root.findall(f"{{{NS_ATOM}}}entry"):
        status = entry.findtext(f"{{{NS_CAP}}}status", "") or ""
        if status and status != "Actual":
            continue
        infos = entry.findall(f"{{{NS_CAP}}}info")
        if not infos:
            continue
        primary, alt = meteoalarm._pick_info_blocks(infos, preferred_prefix)
        alerts.append(meteoalarm._entry_to_alert(entry, primary, alt))
    return alerts


def test_parses_three_active_entries(feed_de_xml):
    alerts = _parse(feed_de_xml)
    # Fixture has 3 actual entries plus one Test status that is filtered.
    assert len(alerts) == 3
    for a in alerts:
        assert a.provider == "meteoalarm"


def test_identifier_hashing_stable(feed_de_xml):
    a1 = _parse(feed_de_xml)
    a2 = _parse(feed_de_xml)
    assert [a.id for a in a1] == [a.id for a in a2]
    # All IDs are 12 hex chars
    for a in a1:
        assert len(a.id) == 12
        assert all(c in "0123456789abcdef" for c in a.id)


def test_severity_passthrough_for_normalization(feed_de_xml):
    alerts = _parse(feed_de_xml)
    wind = next(a for a in alerts if a.event == "Wind")
    assert wind.severity == "Moderate"
    (out,) = normalize_alerts([wind])
    assert out.severity_normalized == "moderate"


def test_language_merge_de_primary_en_alt(feed_de_xml):
    alerts = _parse(feed_de_xml, preferred_prefix="de")
    wind = next(a for a in alerts if a.event == "Wind")
    assert wind.language.startswith("de")
    assert wind.headline.startswith("Sturm")
    assert wind.headline_alt
    assert wind.language_alt.startswith("en")
    assert "Storm" in wind.headline_alt


def test_language_merge_en_primary_when_preferred_missing(feed_de_xml):
    alerts = _parse(feed_de_xml, preferred_prefix="fr")
    wind = next(a for a in alerts if a.event == "Wind")
    # No fr; falls back to en (the generic English fallback rule).
    assert wind.language.startswith("en")


def test_polygon_to_geojson_lon_lat(feed_de_xml):
    alerts = _parse(feed_de_xml)
    wind = next(a for a in alerts if a.event == "Wind")
    assert wind.geometry is not None
    assert wind.geometry["type"] == "Polygon"
    coords = wind.geometry["coordinates"][0]
    # Closed ring with at least four points
    assert len(coords) >= 4
    assert coords[0] == coords[-1]
    # GeoJSON order: each point is [lon, lat]
    lon, lat = coords[0]
    assert -180 <= lon <= 180
    assert -90 <= lat <= 90


def test_awareness_level_in_parameters(feed_de_xml):
    alerts = _parse(feed_de_xml)
    wind = next(a for a in alerts if a.event == "Wind")
    assert wind.parameters is not None
    assert wind.parameters.get("awareness_level") == "2; yellow; Moderate"


def test_test_status_entry_filtered(feed_de_xml):
    # Fixture includes a status="Test" entry; ensure it's not in the output.
    alerts = _parse(feed_de_xml)
    events = {a.event for a in alerts}
    assert "Test Hazard" not in events


def test_entry_without_polygon_keeps_no_geometry(feed_de_xml):
    alerts = _parse(feed_de_xml)
    snow = next(a for a in alerts if a.event == "Snow/Ice")
    assert snow.geometry is None


def test_cancel_msg_type_passes_through(feed_de_xml):
    alerts = _parse(feed_de_xml)
    cancel = next(a for a in alerts if a.event == "Forest fire")
    assert cancel.msg_type == "Cancel"
