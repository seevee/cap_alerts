"""Tests for area-geocode collection in the shared CAP parser (``cap.py``)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load_cap_parser() -> types.ModuleType:
    """Load ``providers/cap.py`` standalone, outside the package namespace.

    The parser deliberately depends on nothing else in the package, so it needs
    no ``cap_alerts.providers`` stub. Registering one here would shadow the real
    package for every later-collected test module (this file sorts before the
    coordinator/sensor tests, which do ``from .providers import AlertProvider``).
    """
    full = "cap_alerts_cap_parser"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / "cap.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_cap_mod = _load_cap_parser()
parse_cap_alert = _cap_mod.parse_cap_alert


def _cap_xml(areas: str) -> str:
    """Minimal CAP 1.2 document wrapping the given ``<area>`` blocks."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>test-geocodes</identifier>
  <sender>test@example.org</sender>
  <sent>2026-07-29T12:00:00-00:00</sent>
  <status>Actual</status>
  <msgType>Alert</msgType>
  <scope>Public</scope>
  <info>
    <language>en-CA</language>
    <category>Met</category>
    <event>Test Event</event>
    <urgency>Expected</urgency>
    <severity>Moderate</severity>
    <certainty>Likely</certainty>
{areas}
  </info>
</alert>
"""


def _geocode(scheme: str, value: str) -> str:
    return (
        f"      <geocode><valueName>{scheme}</valueName>"
        f"<value>{value}</value></geocode>"
    )


def test_parse_dedupes_geocode_value_repeated_across_areas():
    # The same CLC value in two <area> blocks is one code, not two.
    clc = "layer:EC-MSC-SMC:1.0:CLC"
    areas = "\n".join(
        [
            "    <area>",
            "      <areaDesc>Zone A</areaDesc>",
            _geocode(clc, "071100"),
            "    </area>",
            "    <area>",
            "      <areaDesc>Zone B</areaDesc>",
            _geocode(clc, "071100"),
            _geocode(clc, "071200"),
            "    </area>",
        ]
    )
    doc = parse_cap_alert(_cap_xml(areas))
    assert doc is not None
    assert doc.infos[0].geocodes[clc] == ["071100", "071200"]


def test_parse_collects_every_scheme_in_document_order():
    clc = "layer:EC-MSC-SMC:1.0:CLC"
    sgc = "profile:CAP-CP:Location:0.3"
    areas = "\n".join(
        [
            "    <area>",
            "      <areaDesc>Zone A</areaDesc>",
            _geocode(clc, "071100"),
            _geocode(sgc, "3506008"),
            "    </area>",
            "    <area>",
            "      <areaDesc>Zone B</areaDesc>",
            _geocode(sgc, "3506011"),
            _geocode("SAME", "012345"),
            "    </area>",
        ]
    )
    doc = parse_cap_alert(_cap_xml(areas))
    assert doc is not None
    geocodes = doc.infos[0].geocodes
    assert list(geocodes) == [clc, sgc, "SAME"]
    assert geocodes[sgc] == ["3506008", "3506011"]
    assert geocodes["SAME"] == ["012345"]
