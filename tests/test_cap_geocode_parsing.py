"""Tests for area-geocode collection in the shared CAP parser (``cap.py``)."""

from __future__ import annotations

from custom_components.cap_alerts.providers.cap import parse_cap_alert


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
