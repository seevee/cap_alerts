"""Tests for ECCC provider — CAP-body parity (description, timestamps, lifecycle)."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from custom_components.cap_alerts.providers import cap as _cap_mod
from custom_components.cap_alerts.providers import cap_content_cache as _cap_cache_mod
from custom_components.cap_alerts.providers import eccc as _eccc_mod

_FIXTURES = Path(__file__).parent / "fixtures"


CAPContentCache = _cap_cache_mod.CAPContentCache
ECCCProvider = _eccc_mod.ECCCProvider
_pick_cap_link = _eccc_mod._pick_cap_link
_parse_georss_polygons = _eccc_mod._parse_georss_polygons
_point_in_polygons = _eccc_mod._point_in_polygons
_parse_cap_alert = _cap_mod.parse_cap_alert
_select_info = _eccc_mod._select_info
_select_region_info = _eccc_mod._select_region_info
_language_matches = _eccc_mod._language_matches
_location_status = _eccc_mod._location_status
_is_terminal_info = _eccc_mod._is_terminal_info
_resolve_chain_leaves = _cap_mod.resolve_chain_leaves
_bilingual_key = _eccc_mod._bilingual_key
_fallback_id = _eccc_mod._fallback_id
_build_alert_from_cap = _eccc_mod._build_alert_from_cap
_is_marine_eccc = _eccc_mod._is_marine_eccc
_matches_province_sgc = _eccc_mod._matches_province_sgc
_bbox_of_polygons = _eccc_mod._bbox_of_polygons
_province_bbox_intersects = _eccc_mod._province_bbox_intersects
build_alerts_from_cap_docs = _eccc_mod.build_alerts_from_cap_docs
doc_matches_region = _eccc_mod.doc_matches_region
_resolve_feed_urls = _eccc_mod.resolve_feed_urls
_entry_oid = _eccc_mod._entry_oid
_headline_to_event = _eccc_mod._headline_to_event
_best_event_name = _eccc_mod._best_event_name
CAPDoc = _cap_mod.CAPDoc
CAPInfoDoc = _cap_mod.CAPInfoDoc


from tests.conftest import StubSession  # noqa: E402 — after module setup


# ---------------------------------------------------------------------------
# Fixture loading helpers
# ---------------------------------------------------------------------------


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _atom_xml() -> str:
    return _fixture("eccc_naad_atom.xml")


def _cap_responses() -> dict[str, str]:
    return {
        "https://cap.naad-adna.pelmorex.com/alerts/en_new_1.cap": _fixture(
            "eccc_cap_en_new_1.xml"
        ),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_new_1.cap": _fixture(
            "eccc_cap_fr_new_1.xml"
        ),
        "https://cap.naad-adna.pelmorex.com/alerts/en_update_1.cap": _fixture(
            "eccc_cap_en_update_1.xml"
        ),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_update_1.cap": _fixture(
            "eccc_cap_fr_update_1.xml"
        ),
        # BC entry — in province=ON mode its polygon bbox misses Ontario, so the
        # bbox gate drops it pre-fetch and this response is never requested. In
        # GPS mode it is fetched normally.
        "https://cap.naad-adna.pelmorex.com/alerts/bc_wind_1.cap": _fixture(
            "eccc_cap_bc_wind_1.xml"
        ),
    }


# ---------------------------------------------------------------------------
# Atom namespace helper for building test elements
# ---------------------------------------------------------------------------

from xml.etree.ElementTree import Element, SubElement  # noqa: E402

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_GEORSS = "http://www.georss.org/georss"


def _atom_entry_with_links(
    cap_url: str = "",
    html_url: str = "",
    cap_type: str = "application/cap+xml",
    html_type: str = "text/html",
) -> Element:
    entry = Element(f"{{{NS_ATOM}}}entry")
    if cap_url:
        link = SubElement(entry, f"{{{NS_ATOM}}}link")
        link.set("rel", "alternate")
        link.set("type", cap_type)
        link.set("href", cap_url)
    if html_url:
        link = SubElement(entry, f"{{{NS_ATOM}}}link")
        link.set("rel", "alternate")
        link.set("type", html_type)
        link.set("href", html_url)
    return entry


# ---------------------------------------------------------------------------
# _pick_cap_link tests
# ---------------------------------------------------------------------------


def test_pick_cap_link_prefers_application_cap_xml():
    entry = _atom_entry_with_links(cap_url="https://example.com/alert.cap")
    cap_url, web_url = _pick_cap_link(entry)
    assert cap_url == "https://example.com/alert.cap"
    assert web_url == ""


def test_pick_cap_link_separates_html_alternate_for_web():
    entry = _atom_entry_with_links(
        cap_url="https://example.com/alert.cap",
        html_url="https://example.com/alert.html",
    )
    cap_url, web_url = _pick_cap_link(entry)
    assert cap_url == "https://example.com/alert.cap"
    assert web_url == "https://example.com/alert.html"


def test_pick_cap_link_falls_back_to_extension():
    entry = Element(f"{{{NS_ATOM}}}entry")
    link = SubElement(entry, f"{{{NS_ATOM}}}link")
    link.set("href", "https://example.com/alert.cap")
    link.set("rel", "alternate")

    cap_url, _ = _pick_cap_link(entry)
    assert cap_url == "https://example.com/alert.cap"


def test_pick_cap_link_extension_xml_fallback():
    entry = Element(f"{{{NS_ATOM}}}entry")
    link = SubElement(entry, f"{{{NS_ATOM}}}link")
    link.set("href", "https://example.com/alert.xml")
    link.set("rel", "alternate")

    cap_url, _ = _pick_cap_link(entry)
    assert cap_url == "https://example.com/alert.xml"


# ---------------------------------------------------------------------------
# georss polygon parsing / GPS matching tests
# ---------------------------------------------------------------------------


def _entry_with_polygons(*polygon_texts: str) -> Element:
    """Build an Atom entry carrying one <georss:polygon> per supplied text."""
    entry = Element(f"{{{NS_ATOM}}}entry")
    for text in polygon_texts:
        poly = SubElement(entry, f"{{{NS_GEORSS}}}polygon")
        poly.text = text
    return entry


# Two disjoint squares: A near the origin, B over the Canadian prairies.
# georss text is whitespace-separated "lat lon lat lon ..." pairs.
_POLYGON_A = "0 0 0 1 1 1 1 0 0 0"
_POLYGON_B = "50 -100 50 -99 51 -99 51 -100 50 -100"
# A point inside B only (not A).
_PT_IN_B = (50.5, -99.5)


def test_parse_georss_polygons_returns_all_polygons():
    entry = _entry_with_polygons(_POLYGON_A, _POLYGON_B)
    polygons = _parse_georss_polygons(entry)
    assert len(polygons) == 2


def test_parse_georss_polygons_empty_when_none_present():
    entry = Element(f"{{{NS_ATOM}}}entry")
    assert _parse_georss_polygons(entry) == []


def test_point_in_polygons_matches_a_non_first_polygon():
    """Regression: a point inside the second polygon must match.

    The pre-fix code only inspected the first <georss:polygon>, so an alert
    whose matching area was any polygon other than the first was wrongly
    filtered out.
    """
    lat, lon = _PT_IN_B
    entry = _entry_with_polygons(_POLYGON_A, _POLYGON_B)
    polygons = _parse_georss_polygons(entry)

    assert _point_in_polygons(lat, lon, polygons) is True
    # Demonstrates the old single-polygon behavior would have dropped it.
    assert _point_in_polygons(lat, lon, polygons[:1]) is False


def test_point_in_polygons_false_when_outside_all():
    entry = _entry_with_polygons(_POLYGON_A, _POLYGON_B)
    polygons = _parse_georss_polygons(entry)
    assert _point_in_polygons(10.0, 10.0, polygons) is False


def test_point_in_polygons_false_for_empty():
    assert _point_in_polygons(*_PT_IN_B, []) is False


# ---------------------------------------------------------------------------
# _parse_cap_alert tests
# ---------------------------------------------------------------------------


def test_parse_cap_alert_returns_full_doc():
    xml = _fixture("eccc_cap_en_update_1.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    assert doc.identifier == "urn:oid:2.49.0.1.124.test.2026.UPD.EN"
    assert doc.sender == "CWTO"
    assert doc.sent == "2026-01-15T12:00:00-05:00"
    assert doc.msg_type == "Update"
    assert doc.status == "Actual"
    assert len(doc.infos) == 1
    info = doc.infos[0]
    assert info.language == "en-CA"
    assert info.event == "Freezing Drizzle Advisory"
    assert info.headline == "Freezing Drizzle Advisory continued"
    assert "continues" in info.description
    assert info.expires == "2026-01-15T20:00:00-05:00"
    assert info.event_codes.get("profile:CAP-CP:Event:0.4") == "freezing-drizzle"
    assert info.parameters.get("alertColourLevel") == "Yellow"
    assert len(info.polygons) == 1
    assert info.area_desc == "Ottawa and vicinity"


def test_parse_cap_alert_returns_none_on_malformed_xml():
    doc = _parse_cap_alert("this is not xml <<>>")
    assert doc is None


def test_parse_cap_alert_tolerates_newline_separated_references():
    xml = _fixture("eccc_cap_en_update_1.xml").replace(
        "CWTO,urn:oid:2.49.0.1.124.test.2026.NEW.EN,2026-01-15T08:00:00-05:00",
        "CWTO,urn:oid:2.49.0.1.124.test.2026.NEW.EN,2026-01-15T08:00:00-05:00\n"
        "CWTO,urn:oid:2.49.0.1.124.test.2026.OLD.EN,2026-01-15T06:00:00-05:00",
    )
    doc = _parse_cap_alert(xml)
    assert doc is not None
    assert len(doc.references) == 2
    assert doc.references[0][1] == "urn:oid:2.49.0.1.124.test.2026.NEW.EN"
    assert doc.references[1][1] == "urn:oid:2.49.0.1.124.test.2026.OLD.EN"


# ---------------------------------------------------------------------------
# _language_matches tests
# ---------------------------------------------------------------------------


def test_language_matches_exact_match():
    assert _language_matches("en-CA", "en-CA") is True
    assert _language_matches("fr-CA", "fr-CA") is True


def test_language_matches_bare_primary_against_region():
    """Bare 'en' info block should match preferred 'en-CA'."""
    assert _language_matches("en", "en-CA") is True
    assert _language_matches("en-CA", "en") is True


def test_language_matches_different_primary_subtags():
    """en-CA and fr-CA must not match each other."""
    assert _language_matches("en-CA", "fr-CA") is False
    assert _language_matches("fr-CA", "en-CA") is False


def test_language_matches_empty_strings():
    assert _language_matches("", "en-CA") is False
    assert _language_matches("en-CA", "") is False
    assert _language_matches("", "") is False


def test_language_matches_case_insensitive():
    assert _language_matches("EN-CA", "en-ca") is True
    assert _language_matches("en-CA", "EN-ca") is True


# ---------------------------------------------------------------------------
# _select_info tests
# ---------------------------------------------------------------------------


def test_select_info_picks_matching_language():
    doc = CAPDoc()
    en_info = CAPInfoDoc(language="en-CA", event="Freeze")
    fr_info = CAPInfoDoc(language="fr-CA", event="Gel")
    doc.infos = [en_info, fr_info]

    assert _select_info(doc, "en-CA") is en_info
    assert _select_info(doc, "fr-CA") is fr_info


def test_select_info_falls_back_to_first_when_no_match():
    doc = CAPDoc()
    doc.infos = [CAPInfoDoc(language="en-CA", event="Freeze")]
    assert _select_info(doc, "de-DE").language == "en-CA"


def test_select_info_returns_empty_on_no_infos():
    doc = CAPDoc()
    info = _select_info(doc, "en-CA")
    assert info.event == ""


# ---------------------------------------------------------------------------
# _resolve_chain_leaves tests
# ---------------------------------------------------------------------------


def test_resolve_chain_leaves_drops_superseded_new():
    new_doc = CAPDoc(
        identifier="urn:test.NEW",
        sent="2026-01-15T08:00:00-05:00",
    )
    upd_doc = CAPDoc(
        identifier="urn:test.UPD",
        sent="2026-01-15T12:00:00-05:00",
        references=[("CWTO", "urn:test.NEW", "2026-01-15T08:00:00-05:00")],
    )
    leaves = _resolve_chain_leaves([new_doc, upd_doc])
    assert len(leaves) == 1
    assert leaves[0].identifier == "urn:test.UPD"


def test_resolve_chain_leaves_keeps_unsuperseded_new():
    new_doc = CAPDoc(
        identifier="urn:test.NEW",
        sent="2026-01-15T08:00:00-05:00",
    )
    leaves = _resolve_chain_leaves([new_doc])
    assert len(leaves) == 1
    assert leaves[0].identifier == "urn:test.NEW"


def test_resolve_chain_leaves_returns_all_when_no_references():
    doc_a = CAPDoc(identifier="urn:test.A", sent="2026-01-15T08:00:00-05:00")
    doc_b = CAPDoc(identifier="urn:test.B", sent="2026-01-15T09:00:00-05:00")
    leaves = _resolve_chain_leaves([doc_a, doc_b])
    assert len(leaves) == 2


# ---------------------------------------------------------------------------
# _bilingual_key tests
# ---------------------------------------------------------------------------


def _make_key_doc_info(
    sender: str = "CWTO",
    sent: str = "2026-01-15T12:00:00-05:00",
    event_code: str = "freezing-drizzle",
    polygon: list | None = None,
    area_desc: str = "",
) -> tuple[CAPDoc, CAPInfoDoc]:
    doc = CAPDoc(sender=sender, sent=sent)
    info = CAPInfoDoc(
        event_codes={"profile:CAP-CP:Event:0.4": event_code} if event_code else {},
        area_desc=area_desc,
    )
    if polygon is not None:
        info.polygons = [polygon]
    return doc, info


def test_bilingual_key_groups_en_fr_siblings():
    polygon = [
        [-76.0, 45.0],
        [-75.5, 45.0],
        [-75.5, 45.5],
        [-76.0, 45.5],
        [-76.0, 45.0],
    ]
    doc_en = CAPDoc(sender="CWTO", sent="2026-01-15T12:00:00-05:00")
    info_en = CAPInfoDoc(
        language="en-CA",
        event_codes={"profile:CAP-CP:Event:0.4": "freezing-drizzle"},
        polygons=[polygon],
    )
    doc_fr = CAPDoc(sender="CWTO", sent="2026-01-15T12:00:00-05:00")
    info_fr = CAPInfoDoc(
        language="fr-CA",
        event_codes={"profile:CAP-CP:Event:0.4": "freezing-drizzle"},
        polygons=[polygon],
    )
    assert _bilingual_key(doc_en, info_en) == _bilingual_key(doc_fr, info_fr)


def test_bilingual_key_distinct_for_different_polygon():
    polygon_a = [
        [-76.0, 45.0],
        [-75.5, 45.0],
        [-75.5, 45.5],
        [-76.0, 45.5],
        [-76.0, 45.0],
    ]
    polygon_b = [
        [-77.0, 45.0],
        [-76.5, 45.0],
        [-76.5, 45.5],
        [-77.0, 45.5],
        [-77.0, 45.0],
    ]
    doc, info_a = _make_key_doc_info(polygon=polygon_a)
    _, info_b = _make_key_doc_info(polygon=polygon_b)
    info_b.event_codes = info_a.event_codes.copy()
    assert _bilingual_key(doc, info_a) != _bilingual_key(doc, info_b)


def test_bilingual_key_distinct_for_different_event_code():
    polygon = [
        [-76.0, 45.0],
        [-75.5, 45.0],
        [-75.5, 45.5],
        [-76.0, 45.5],
        [-76.0, 45.0],
    ]
    doc_a, info_a = _make_key_doc_info(event_code="freezing-drizzle", polygon=polygon)
    doc_b, info_b = _make_key_doc_info(event_code="blizzard", polygon=polygon)
    assert _bilingual_key(doc_a, info_a) != _bilingual_key(doc_b, info_b)


# ---------------------------------------------------------------------------
# _fallback_id tests
# ---------------------------------------------------------------------------


def test_fallback_id_distinct_per_language():
    atom_id = "https://www.naad-adna.pelmorex.com/uuid-1"
    id_en = _fallback_id(atom_id, "en-CA")
    id_fr = _fallback_id(atom_id, "fr-CA")
    assert id_en != id_fr


def test_fallback_id_is_deterministic():
    atom_id = "https://www.naad-adna.pelmorex.com/uuid-1"
    assert _fallback_id(atom_id, "en-CA") == _fallback_id(atom_id, "en-CA")


# ---------------------------------------------------------------------------
# _build_alert_from_cap tests
# ---------------------------------------------------------------------------


def _make_alert_from_update_fixture() -> Any:
    xml = _fixture("eccc_cap_en_update_1.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    info = _select_info(doc, "en-CA")
    atom_metadata = {"atom_id": "https://example.com/atom-id", "language": "en-CA"}
    alert_id = _bilingual_key(doc, info)
    return _build_alert_from_cap(
        doc, info, atom_metadata, "https://html.fallback/", alert_id
    )


def test_build_alert_from_cap_populates_all_fields():
    alert = _make_alert_from_update_fixture()
    assert alert.identifier == "urn:oid:2.49.0.1.124.test.2026.UPD.EN"
    assert alert.sender == "CWTO"
    assert alert.sender_name == "Environment and Climate Change Canada"
    assert alert.event == "Freezing Drizzle Advisory"
    assert alert.msg_type == "Update"
    assert alert.sent == "2026-01-15T12:00:00-05:00"
    assert alert.effective == "2026-01-15T12:00:00-05:00"
    assert alert.onset == "2026-01-15T13:00:00-05:00"
    assert alert.expires == "2026-01-15T20:00:00-05:00"
    assert alert.headline == "Freezing Drizzle Advisory continued"
    assert "continues" in alert.description
    assert alert.instruction is not None and "outdoors" in alert.instruction
    assert alert.web == "https://www.weather.gc.ca/warnings/alert_en.html"
    assert alert.area_desc == "Ottawa and vicinity"
    assert alert.geometry is not None
    assert alert.geometry["type"] == "Polygon"
    assert alert.provider == "eccc"


def test_build_alert_from_cap_populates_geocode_clc():
    # CLC area geocode (layer:EC-MSC-SMC:1.0:CLC in the body) is carried onto
    # the alert. Ottawa is a land zone -> province-numbered prefix (07), not
    # marine ("00").
    alert = _make_alert_from_update_fixture()
    assert alert.geocode_clc == ("071100",)


def test_build_alert_from_cap_populates_full_geocode_container():
    # Every area geocode scheme in the CAP body is surfaced. Both of ECCC's
    # carry a version in their valueName, so both publish under their canonical
    # short name — CLC and the StatCan SGC code that province filtering matches.
    alert = _make_alert_from_update_fixture()
    assert alert.geocodes == {
        "CLC": ("071100",),
        "SGC": ("3506008",),
    }


def test_build_alert_from_cap_populates_geocode_sgc():
    # 35 = Ontario; reachable through the accessor, and inspectable from the
    # entity attributes through the container that publishes it.
    alert = _make_alert_from_update_fixture()
    assert alert.geocode_sgc == ("3506008",)
    attrs = alert.to_attributes()
    assert attrs["geocodes"]["SGC"] == ["3506008"]
    assert "geocode_sgc" not in attrs


def test_build_alert_from_cap_geocode_sgc_absent_stays_sparse():
    # The marine fixture carries CLC only -> no SGC alias, and no attribute key.
    xml = _fixture("eccc_cap_en_marine.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    info = _select_info(doc, "en-CA")
    alert = _build_alert_from_cap(
        doc, info, {"atom_id": "", "language": "en-CA"}, "", _bilingual_key(doc, info)
    )
    assert alert.geocode_sgc == ()
    assert "geocode_sgc" not in alert.to_attributes()


def test_build_alert_from_cap_geocode_clc_empty_when_absent():
    # No CLC geocode in the SPS fixture -> field stays an empty tuple (omitted
    # from to_attributes by the sparse serializer).
    xml = _fixture("eccc_cap_en_sps.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    info = _select_info(doc, "en-CA")
    alert = _build_alert_from_cap(
        doc, info, {"atom_id": "", "language": "en-CA"}, "", _bilingual_key(doc, info)
    )
    assert alert.geocode_clc == ()


def test_is_marine_eccc_true_for_water_zone_prefix():
    assert _is_marine_eccc(("004310",)) is True
    assert _is_marine_eccc(("071100", "004410")) is True


def test_is_marine_eccc_false_for_land_and_empty():
    assert _is_marine_eccc(("071100",)) is False
    assert _is_marine_eccc(()) is False


def test_build_alert_from_cap_marine_zone_sets_is_marine():
    # CLC "004310" (Lake Ontario) is a water zone -> is_marine True, and the
    # CLC geocode is carried onto the alert.
    xml = _fixture("eccc_cap_en_marine.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    info = _select_info(doc, "en-CA")
    alert = _build_alert_from_cap(
        doc, info, {"atom_id": "", "language": "en-CA"}, "", _bilingual_key(doc, info)
    )
    assert alert.geocode_clc == ("004310",)
    assert alert.is_marine is True
    # Surfaced as an attribute only when True.
    assert alert.to_attributes().get("is_marine") is True


def test_build_alert_from_cap_land_zone_not_marine():
    # Ottawa land update fixture (CLC "071100") -> is_marine False, and the
    # attribute is omitted entirely.
    alert = _make_alert_from_update_fixture()
    assert alert.is_marine is False
    assert "is_marine" not in alert.to_attributes()


def test_build_alert_from_cap_routes_eventcode_to_parameters():
    alert = _make_alert_from_update_fixture()
    # CAP-CP event codes must land in parameters, NOT event_code_same/event_code_nws
    assert alert.event_code_same == ""
    assert alert.event_code_nws == ""
    assert alert.parameters is not None
    assert alert.parameters.get("profile:CAP-CP:Event:0.4") == "freezing-drizzle"
    assert alert.parameters.get("alertColourLevel") == "Yellow"


def test_build_alert_from_cap_uses_first_category():
    xml = _fixture("eccc_cap_en_new_1.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    info = _select_info(doc, "en-CA")
    alert = _build_alert_from_cap(
        doc, info, {"atom_id": "", "language": "en-CA"}, "", _bilingual_key(doc, info)
    )
    assert alert.category == "Met"


def test_build_alert_from_cap_uses_fallback_web_when_no_info_web():
    xml = _fixture("eccc_cap_en_new_1.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    info = _select_info(doc, "en-CA")
    # Remove the web field
    info.web = ""
    alert = _build_alert_from_cap(
        doc,
        info,
        {"atom_id": "", "language": "en-CA"},
        "https://fallback.url/",
        _bilingual_key(doc, info),
    )
    assert alert.web == "https://fallback.url/"


def test_build_alert_from_cap_references_populated():
    alert = _make_alert_from_update_fixture()
    assert "urn:oid:2.49.0.1.124.test.2026.NEW.EN" in alert.references


# ---------------------------------------------------------------------------
# _headline_to_event / _best_event_name tests
# ---------------------------------------------------------------------------


def test_headline_to_event_strips_in_effect():
    assert (
        _headline_to_event("Special Weather Statement in effect")
        == "Special Weather Statement"
    )


def test_headline_to_event_strips_in_effect_for_area():
    result = _headline_to_event("Special Weather Statement in effect for James Bay")
    assert result == "Special Weather Statement"


def test_headline_to_event_strips_continued():
    assert (
        _headline_to_event("Freezing Drizzle Advisory continued")
        == "Freezing Drizzle Advisory"
    )


def test_headline_to_event_strips_french_en_vigueur():
    result = _headline_to_event("Bulletin météorologique spécial en vigueur")
    assert result == "Bulletin météorologique spécial"


def test_headline_to_event_strips_french_en_vigueur_pour():
    result = _headline_to_event(
        "Bulletin météorologique spécial en vigueur pour la Baie James"
    )
    assert result == "Bulletin météorologique spécial"


def test_headline_to_event_returns_full_string_when_no_suffix_matches():
    assert _headline_to_event("Some Unknown Headline") == "Some Unknown Headline"


def test_headline_to_event_strips_trailing_hyphen_from_colour_warning():
    assert (
        _headline_to_event("Yellow Warning - Wind - in effect")
        == "Yellow Warning - Wind"
    )


def test_headline_to_event_strips_trailing_hyphen_french():
    assert (
        _headline_to_event("avertissement jaune - vent - en vigueur")
        == "avertissement jaune - vent"
    )


def test_best_event_name_prefers_alert_name_parameter():
    # ECCC's canonical event name is in the CAP parameter — it has no status
    # suffix and no leftover separators, so we should prefer it over headline.
    result = _best_event_name(
        "wind",
        "yellow warning - wind - in effect",
        atom_title="yellow warning - wind - in effect",
        parameters={"layer:EC-MSC-SMC:1.0:Alert_Name": "yellow warning - wind"},
    )
    assert result == "Yellow Warning - Wind"


def test_best_event_name_alert_name_parameter_preserves_proper_case():
    result = _best_event_name(
        "",
        "",
        parameters={"layer:EC-MSC-SMC:1.0:Alert_Name": "Tornado Warning"},
    )
    assert result == "Tornado Warning"


def test_best_event_name_v1_1_alert_name_wins_over_v1_0():
    # When both layer versions are present we prefer 1.1.
    result = _best_event_name(
        "",
        "",
        parameters={
            "layer:EC-MSC-SMC:1.0:Alert_Name": "stale name",
            "layer:EC-MSC-SMC:1.1:Alert_Name": "fresh name",
        },
    )
    assert result == "Fresh Name"


def test_best_event_name_falls_back_to_headline_when_parameter_missing():
    # No Alert_Name parameter — existing headline path still works.
    result = _best_event_name(
        "weather",
        "Special Weather Statement in effect",
        parameters={"some:other:param": "value"},
    )
    assert result == "Special Weather Statement"


def test_best_event_name_extracts_from_headline_for_generic_weather():
    result = _best_event_name("weather", "Special Weather Statement in effect")
    assert result == "Special Weather Statement"


def test_best_event_name_preserves_specific_event():
    result = _best_event_name(
        "Freezing Drizzle Advisory", "Freezing Drizzle Advisory in effect"
    )
    assert result == "Freezing Drizzle Advisory"


def test_best_event_name_title_cases_event_when_no_headline():
    # With no headline or atom title to recover from, title-case the raw event.
    assert _best_event_name("weather", "") == "Weather"


def test_best_event_name_extracts_from_headline_for_lowercase_event():
    # ECCC <event> may be "special weather statement" (all-lowercase); headline has proper casing.
    result = _best_event_name(
        "special weather statement",
        "Special Weather Statement in effect for James Bay",
    )
    assert result == "Special Weather Statement"


def test_best_event_name_prefers_atom_title_when_cap_body_is_lowercase():
    # Live ECCC sometimes returns lowercase CAP <event> AND <headline>;
    # only the parent Atom <title> carries proper casing.
    result = _best_event_name(
        "special weather statement",
        "special weather statement in effect",
        atom_title="Special Weather Statement in effect",
    )
    assert result == "Special Weather Statement"


def test_best_event_name_title_cases_when_all_lowercase():
    # Production ECCC: Atom title, headline, and event all lowercase.
    # We title-case the suffix-stripped headline as a last resort.
    result = _best_event_name(
        "special weather statement",
        "special weather statement in effect",
        atom_title="special weather statement in effect for james bay",
    )
    # The headline-suffix stripper finds " in effect for " and strips it,
    # then we title-case the remainder.
    assert result == "Special Weather Statement"


def test_best_event_name_title_cases_event_only():
    # No headline, no atom title — title-case the raw event.
    assert (
        _best_event_name("special weather statement", "", "")
        == "Special Weather Statement"
    )


def test_build_alert_from_cap_derives_sps_event_from_headline():
    xml = _fixture("eccc_cap_en_sps.xml")
    doc = _parse_cap_alert(xml)
    assert doc is not None
    info = _select_info(doc, "en-CA")
    assert info.event == "weather"
    assert info.headline == "Special Weather Statement in effect"
    alert = _build_alert_from_cap(
        doc, info, {"atom_id": "", "language": "en-CA"}, "", _bilingual_key(doc, info)
    )
    assert alert.event == "Special Weather Statement"


# ---------------------------------------------------------------------------
# Full provider flow test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eccc_provider_full_flow():
    """Four CAP entries (NEW+UPDATE × en/fr) → one merged bilingual alert."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    cache = CAPContentCache()
    config = {"province": "ON"}
    options = {"language": "en-CA", "feed_source": "alertready"}

    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, config, options, cap_content_cache=cache
    )

    assert len(alerts) == 1
    alert = alerts[0]

    # CAP-body fields
    assert alert.event == "Freezing Drizzle Advisory"
    assert alert.msg_type == "Update"
    assert alert.sent == "2026-01-15T12:00:00-05:00"
    assert alert.expires == "2026-01-15T20:00:00-05:00"
    assert alert.headline == "Freezing Drizzle Advisory continued"
    assert "continues" in alert.description
    assert alert.parameters is not None
    assert alert.parameters.get("profile:CAP-CP:Event:0.4") == "freezing-drizzle"
    # Bilingual alt fields (fr-CA sibling merged)
    assert alert.headline_alt != ""
    assert "maintenu" in alert.headline_alt or "verglaçante" in alert.headline_alt
    assert alert.language == "en-CA"
    assert alert.language_alt == "fr-CA"
    # The sibling's event name rides along so a French-presented alert can
    # still be classified for an icon (#91).
    assert alert.event_alt != ""


@pytest.mark.asyncio
async def test_eccc_provider_filters_expired_alert():
    """CAP expires in the past → normalize tags phase=expired."""
    from custom_components.cap_alerts.normalize import normalize_alerts

    # Create a CAP file with past expires
    xml_expired = _fixture("eccc_cap_en_update_1.xml").replace(
        "<expires>2026-01-15T20:00:00-05:00</expires>",
        "<expires>2020-01-01T00:00:00+00:00</expires>",
    )
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        "https://cap.naad-adna.pelmorex.com/alerts/en_new_1.cap": _fixture(
            "eccc_cap_en_new_1.xml"
        ),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_new_1.cap": _fixture(
            "eccc_cap_fr_new_1.xml"
        ),
        "https://cap.naad-adna.pelmorex.com/alerts/en_update_1.cap": xml_expired,
        "https://cap.naad-adna.pelmorex.com/alerts/fr_update_1.cap": _fixture(
            "eccc_cap_fr_update_1.xml"
        ).replace(
            "<expires>2026-01-15T20:00:00-05:00</expires>",
            "<expires>2020-01-01T00:00:00+00:00</expires>",
        ),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA", "feed_source": "alertready"}
    )

    normalized = normalize_alerts(alerts)
    assert len(normalized) == 1
    assert normalized[0].phase == "expired"


@pytest.mark.asyncio
async def test_eccc_provider_metadata_only_fallback_on_fetch_failure():
    """CAP fetch returns 404 → alert surfaces with Atom-only fields, empty long-form text.

    Uses GPS mode: the entry passed the envelope polygon filter, so its location
    is verified and a metadata-only fallback is safe. Province mode instead fails
    closed on CAP failure (it cannot verify the province without the CAP body).
    """
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        # All CAP files return 404
        "https://cap.naad-adna.pelmorex.com/alerts/en_new_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_new_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/en_update_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_update_1.cap": (404, ""),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    # GPS point inside the Ottawa entries' envelope polygon (lat 45–45.5, lon -76 to -75.5)
    alerts = await provider.async_fetch(
        session,
        {"gps_loc": "45.2,-75.7"},
        {"language": "en-CA", "feed_source": "alertready"},
    )

    # Should surface 4 metadata-only alerts (one per Atom entry, no bilingual merge)
    assert len(alerts) > 0
    for alert in alerts:
        assert alert.headline == ""
        assert alert.description == ""
        assert alert.instruction is None
        assert alert.provider == "eccc"
        # Atom-derived fields present
        assert alert.event != "" or alert.area_desc != ""

    # Atom entry <title> gives properly-cased event names even in the fallback path
    en_events = {a.event for a in alerts if a.language == "en-CA"}
    assert en_events == {"Freezing Drizzle Advisory"}


@pytest.mark.asyncio
async def test_eccc_provider_filters_test_status():
    """Status=Test entry is dropped before any CAP fetch."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA", "feed_source": "alertready"}
    )
    # Test entry should not appear; only the ON freezing drizzle series
    assert all(alert.area_desc != "" for alert in alerts)
    # The test entry's event would be "test alert"
    assert not any("test alert" in (a.event or "").lower() for a in alerts)


@pytest.mark.asyncio
async def test_eccc_provider_filters_foreign_province():
    """BC entry is dropped pre-fetch by the bbox gate; its CAP body is never fetched."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA", "feed_source": "alertready"}
    )
    assert not any("Vancouver" in (a.area_desc or "") for a in alerts)
    # Positive side: the in-province ON alert survives.
    assert any("Ottawa" in (a.area_desc or "") for a in alerts)
    # The BC entry's bbox misses Ontario, so its CAP body is never requested,
    # while the in-province Ottawa CAP bodies are.
    assert (
        "https://cap.naad-adna.pelmorex.com/alerts/bc_wind_1.cap"
        not in session.requested
    )
    assert "https://cap.naad-adna.pelmorex.com/alerts/en_new_1.cap" in session.requested


@pytest.mark.asyncio
async def test_eccc_provider_raises_on_persistently_truncated_feed(monkeypatch):
    """A feed body missing </feed> on every attempt raises UpdateFailed, not ParseError.

    Simulates istio-envoy terminating the ~7 MB chunked stream early: aiohttp
    returns a partial body without raising, so the guard must catch it. The
    fetch retries the bounded number of times, then fails cleanly.
    """
    monkeypatch.setattr(_eccc_mod, "_FEED_RETRY_BACKOFF_S", 0)
    truncated = _atom_xml()[: len(_atom_xml()) // 2]
    responses: dict[str, Any] = {"https://rss.alertready.ca/": truncated}
    session = StubSession(responses)
    provider = ECCCProvider()

    with pytest.raises(_eccc_mod.UpdateFailed, match="truncated feed response"):
        await provider.async_fetch(
            session,
            {"province": "ON"},
            {"language": "en-CA", "feed_source": "alertready"},
        )

    # Retried the full budget; no CAP bodies fetched on a never-parseable feed.
    assert (
        session.requested.count("https://rss.alertready.ca/")
        == _eccc_mod._FEED_FETCH_ATTEMPTS
    )
    assert not any(".cap" in url for url in session.requested)


@pytest.mark.asyncio
async def test_eccc_provider_retries_then_succeeds_on_truncated_feed(monkeypatch):
    """A single truncated feed response recovers on retry within the same poll."""
    monkeypatch.setattr(_eccc_mod, "_FEED_RETRY_BACKOFF_S", 0)
    truncated = _atom_xml()[: len(_atom_xml()) // 2]
    responses: dict[str, Any] = {
        # First GET truncated, second GET complete → parses on attempt 2.
        "https://rss.alertready.ca/": [truncated, _atom_xml()],
        **_cap_responses(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()

    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA", "feed_source": "alertready"}
    )

    # Recovered to the same result as the clean full-flow case.
    assert len(alerts) == 1
    assert alerts[0].event == "Freezing Drizzle Advisory"
    # Feed fetched exactly twice (one retry), then CAP bodies proceed normally.
    assert session.requested.count("https://rss.alertready.ca/") == 2


def test_matches_province_sgc():
    """SGC location prefix decides province; unknown codes and missing key fail."""
    on = {"profile:CAP-CP:Location:0.3": ("3506008", "3558090")}
    bc = {"profile:CAP-CP:Location:0.3": ("5900010",)}
    assert _matches_province_sgc(on, "ON") is True
    assert _matches_province_sgc(on, "on") is True  # case-insensitive
    assert _matches_province_sgc(on, "BC") is False
    assert _matches_province_sgc(bc, "ON") is False
    # No SGC geocode present → no match
    assert _matches_province_sgc({"SAME": ("012345",)}, "ON") is False
    # Unrecognised province code → no match (never matches everything)
    assert _matches_province_sgc(on, "ZZ") is False


# Ontario (Ottawa-ish) and BC (Vancouver Island-ish) rings, [lon, lat] pairs.
_ON_RING = [[-76.0, 45.0], [-75.5, 45.0], [-75.5, 45.5], [-76.0, 45.5], [-76.0, 45.0]]
_BC_RING = [
    [-125.0, 48.0],
    [-123.0, 48.0],
    [-123.0, 50.0],
    [-125.0, 50.0],
    [-125.0, 48.0],
]


def test_bbox_of_polygons_single_ring():
    assert _bbox_of_polygons([_ON_RING]) == (-76.0, 45.0, -75.5, 45.5)


def test_bbox_of_polygons_multiple_rings_unions():
    assert _bbox_of_polygons([_ON_RING, _BC_RING]) == (-125.0, 45.0, -75.5, 50.0)


def test_bbox_of_polygons_empty_is_none():
    assert _bbox_of_polygons([]) is None
    assert _bbox_of_polygons([[]]) is None


def test_province_bbox_intersects_in_province():
    assert _province_bbox_intersects([_ON_RING], "ON") is True


def test_province_bbox_intersects_foreign_province():
    assert _province_bbox_intersects([_BC_RING], "ON") is False
    assert _province_bbox_intersects([_BC_RING], "BC") is True


def test_province_bbox_intersects_case_insensitive():
    assert _province_bbox_intersects([_ON_RING], "on") is True


def test_province_bbox_intersects_fails_open_on_empty_geometry():
    assert _province_bbox_intersects([], "ON") is True


def test_province_bbox_intersects_fails_open_on_unknown_province():
    assert _province_bbox_intersects([_BC_RING], "ZZ") is True


@pytest.mark.asyncio
async def test_eccc_province_fails_closed_on_cap_fetch_failure():
    """Province mode drops entries whose CAP body can't be fetched (no SGC to verify)."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        # Every CAP body 404s → no SGC code available for any entry.
        "https://cap.naad-adna.pelmorex.com/alerts/en_new_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_new_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/en_update_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_update_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/bc_wind_1.cap": (404, ""),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA", "feed_source": "alertready"}
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_eccc_provider_returns_empty_when_no_location_configured():
    """Neither province nor GPS → empty list."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {}, {"language": "en-CA", "feed_source": "alertready"}
    )
    assert alerts == []


# ---------------------------------------------------------------------------
# CAPContentCache tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_serves_cached_url():
    fetch_count = 0

    class CountSession:
        def get(self, url: str, **kw: Any) -> Any:
            class Resp:
                status = 200

                async def text(self) -> str:
                    nonlocal fetch_count
                    fetch_count += 1
                    return "body"

                async def __aenter__(self) -> "Resp":
                    return self

                async def __aexit__(self, *a: Any) -> None:
                    pass

            return Resp()

    cache = CAPContentCache()
    session = CountSession()
    url = "http://test.example/cap.xml"

    r1 = await cache.get_or_fetch(session, url)
    r2 = await cache.get_or_fetch(session, url)

    assert r1 == "body"
    assert r2 == "body"
    assert fetch_count == 1


@pytest.mark.asyncio
async def test_cache_evicts_when_over_capacity():
    # Budget sized to exactly two bodies: eviction is by bytes, not entries,
    # because CAP body size spans two orders of magnitude across sources.
    cache = CAPContentCache(max_bytes=2 * sys.getsizeof("url1"))

    class FixedSession:
        def get(self, url: str, **kw: Any) -> Any:
            body = url.split("/")[-1]  # Use last path segment as body

            class Resp:
                status = 200

                async def text(self) -> str:
                    return body

                async def __aenter__(self) -> "Resp":
                    return self

                async def __aexit__(self, *a: Any) -> None:
                    pass

            return Resp()

    session = FixedSession()
    await cache.get_or_fetch(session, "http://test/url1")
    await cache.get_or_fetch(session, "http://test/url2")
    await cache.get_or_fetch(session, "http://test/url3")

    assert "http://test/url1" not in cache._cache
    assert "http://test/url2" in cache._cache
    assert "http://test/url3" in cache._cache


@pytest.mark.asyncio
async def test_cache_returns_none_on_http_error():
    session = StubSession({"http://test/cap.xml": (503, "")})
    cache = CAPContentCache()
    result = await cache.get_or_fetch(session, "http://test/cap.xml")
    assert result is None


@pytest.mark.asyncio
async def test_cache_returns_none_on_timeout():
    import aiohttp

    session = StubSession({"http://test/cap.xml": lambda: aiohttp.ServerTimeoutError()})
    cache = CAPContentCache()
    result = await cache.get_or_fetch(session, "http://test/cap.xml")
    assert result is None


@pytest.mark.asyncio
async def test_cache_coalesces_concurrent_requests_for_same_url():
    """Two concurrent get_or_fetch calls for the same URL trigger one HTTP GET."""
    fetch_count = 0
    release = asyncio.Event()

    class SlowSession:
        def get(self, url: str, **kw: Any) -> Any:
            class SlowResp:
                status = 200

                async def text(self) -> str:
                    nonlocal fetch_count
                    fetch_count += 1
                    await release.wait()
                    return "slow-body"

                async def __aenter__(self) -> "SlowResp":
                    return self

                async def __aexit__(self, *a: Any) -> None:
                    pass

            return SlowResp()

    cache = CAPContentCache()
    session = SlowSession()
    url = "http://test.example/slow.cap"

    task1 = asyncio.create_task(cache.get_or_fetch(session, url))
    await asyncio.sleep(0)  # Let task1 run to its blocking point
    task2 = asyncio.create_task(cache.get_or_fetch(session, url))
    await asyncio.sleep(0)  # Let task2 run (should see inflight)

    release.set()
    r1, r2 = await asyncio.gather(task1, task2)

    assert r1 == "slow-body"
    assert r2 == "slow-body"
    assert fetch_count == 1


@pytest.mark.asyncio
async def test_cache_inflight_dict_does_not_grow_unbounded():
    """After N misses all resolve, _inflight is empty."""
    session = StubSession({f"http://test/{i}": f"body{i}" for i in range(10)})
    cache = CAPContentCache()
    urls = [f"http://test/{i}" for i in range(10)]
    await asyncio.gather(*[cache.get_or_fetch(session, u) for u in urls])
    assert len(cache._inflight) == 0


# ---------------------------------------------------------------------------
# build_alerts_from_cap_docs — shared doc→alert builder (streaming + backfill)
# ---------------------------------------------------------------------------


def _docs(*names: str) -> list:
    docs = []
    for name in names:
        doc = _parse_cap_alert(_fixture(name))
        assert doc is not None
        docs.append(doc)
    return docs


def test_build_alerts_from_cap_docs_merges_bilingual_pair():
    """en + fr docs sharing a bilingual key merge into one alert (no envelope meta)."""
    docs = _docs("eccc_cap_en_new_1.xml", "eccc_cap_fr_new_1.xml")
    alerts = build_alerts_from_cap_docs(
        docs, province="ON", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
    )
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.language == "en-CA"
    assert alert.language_alt == "fr-CA"
    assert alert.headline_alt != ""
    # Built purely from the CAP body: no Atom id available.
    assert alert.url == ""


def test_build_alerts_from_cap_docs_resolves_revision_chain():
    """An UPDATE referencing a NEW supersedes it; only the leaf survives."""
    docs = _docs(
        "eccc_cap_en_new_1.xml",
        "eccc_cap_fr_new_1.xml",
        "eccc_cap_en_update_1.xml",
        "eccc_cap_fr_update_1.xml",
    )
    alerts = build_alerts_from_cap_docs(
        docs, province="ON", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
    )
    assert len(alerts) == 1
    assert alerts[0].msg_type == "Update"


def test_build_alerts_from_cap_docs_province_sgc_filters_foreign():
    """SGC province check drops a BC doc when ON is configured."""
    docs = _docs("eccc_cap_bc_wind_1.xml")
    assert (
        build_alerts_from_cap_docs(
            docs, province="ON", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
        )
        == []
    )
    # …and keeps it for its own province.
    assert (
        len(
            build_alerts_from_cap_docs(
                docs, province="BC", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
            )
        )
        == 1
    )


def test_build_alerts_from_cap_docs_gps_filters_on_cap_body_polygon():
    """GPS mode filters against the CAP-body polygon (Ottawa ring)."""
    docs = _docs("eccc_cap_en_new_1.xml", "eccc_cap_fr_new_1.xml")
    inside = build_alerts_from_cap_docs(
        docs, province="", gps_lat=45.2, gps_lon=-75.7, preferred_lang="en-CA"
    )
    assert len(inside) == 1
    outside = build_alerts_from_cap_docs(
        docs, province="", gps_lat=10.0, gps_lon=10.0, preferred_lang="en-CA"
    )
    assert outside == []


def test_build_alerts_from_cap_docs_uses_atom_metadata_when_supplied():
    """Backfill/poll path can supply envelope niceties (atom id, web) by identifier."""
    docs = _docs("eccc_cap_en_new_1.xml")
    identifier = docs[0].identifier
    alerts = build_alerts_from_cap_docs(
        docs,
        province="ON",
        gps_lat=None,
        gps_lon=None,
        preferred_lang="en-CA",
        atom_meta_by_id={
            identifier: {"atom_id": "https://atom/id", "language": "en-CA"}
        },
        web_by_id={identifier: "https://fallback.web/"},
    )
    assert len(alerts) == 1
    assert alerts[0].url == "https://atom/id"


@pytest.mark.asyncio
async def test_async_fetch_docs_returns_region_relevant_docs():
    """async_fetch_docs returns parsed CAPDocs; the foreign-province body is never fetched."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    docs = await provider.async_fetch_docs(
        session,
        {"province": "ON"},
        {"language": "en-CA", "feed_source": "alertready"},
        cap_content_cache=CAPContentCache(),
    )
    # ON entries (en/fr × new/update) parse; the BC body is bbox-gated pre-fetch.
    identifiers = {d.identifier for d in docs}
    assert "urn:oid:2.49.0.1.124.test.2026.NEW.EN" in identifiers
    assert (
        "https://cap.naad-adna.pelmorex.com/alerts/bc_wind_1.cap"
        not in session.requested
    )


@pytest.mark.asyncio
async def test_async_fetch_equals_build_over_fetch_docs():
    """async_fetch output matches building alerts from async_fetch_docs (parity)."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        **_cap_responses(),
    }
    provider = ECCCProvider()

    alerts = await provider.async_fetch(
        StubSession(responses),
        {"province": "ON"},
        {"language": "en-CA", "feed_source": "alertready"},
    )
    docs = await provider.async_fetch_docs(
        StubSession(responses),
        {"province": "ON"},
        {"language": "en-CA", "feed_source": "alertready"},
    )
    built = build_alerts_from_cap_docs(
        docs, province="ON", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
    )
    assert {a.id for a in alerts} == {a.id for a in built}
    assert len(alerts) == len(built) == 1


def _doc_with_status(status: str):
    """A minimal ON-province CAP doc carrying the given <status>."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>urn:oid:status-{status}</identifier>"
        "<sender>CWTO</sender><sent>2026-07-22T12:00:00-00:00</sent>"
        f"<status>{status}</status><msgType>Alert</msgType><scope>Public</scope>"
        "<info><language>en-CA</language><category>Met</category>"
        "<event>Wind Warning</event><severity>Moderate</severity>"
        "<headline>Wind Warning in effect</headline>"
        "<area><areaDesc>Ottawa</areaDesc>"
        "<polygon>45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        "<value>3506008</value></geocode>"
        "</area></info></alert>"
    )
    doc = _parse_cap_alert(xml)
    assert doc is not None
    return doc


@pytest.mark.parametrize("status", ["Test", "Exercise", "Draft", "System"])
def test_build_alerts_from_cap_docs_drops_non_actual(status: str):
    """The stream carries the whole NAAD channel; only Actual alerts are real."""
    docs = [_doc_with_status(status)]
    assert (
        build_alerts_from_cap_docs(
            docs, province="ON", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
        )
        == []
    )


def test_build_alerts_from_cap_docs_keeps_actual():
    docs = [_doc_with_status("Actual")]
    assert (
        len(
            build_alerts_from_cap_docs(
                docs, province="ON", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
            )
        )
        == 1
    )


def test_build_alerts_from_cap_docs_fails_open_on_missing_status():
    """<status> is mandatory CAP; a doc without one is malformed, not a test message."""
    doc = _doc_with_status("Actual")
    assert (
        len(
            build_alerts_from_cap_docs(
                [replace(doc, status="")],
                province="ON",
                gps_lat=None,
                gps_lon=None,
                preferred_lang="en-CA",
            )
        )
        == 1
    )


def test_doc_matches_region_province():
    """Streaming admission test: SGC province match."""
    docs = _docs("eccc_cap_bc_wind_1.xml")
    kwargs = {"gps_lat": None, "gps_lon": None, "preferred_lang": "en-CA"}
    assert doc_matches_region(docs[0], province="BC", **kwargs) is True
    assert doc_matches_region(docs[0], province="ON", **kwargs) is False


def test_doc_matches_region_gps_and_status():
    docs = _docs("eccc_cap_en_new_1.xml")
    kwargs = {"province": "", "preferred_lang": "en-CA"}
    assert doc_matches_region(docs[0], gps_lat=45.2, gps_lon=-75.7, **kwargs) is True
    assert doc_matches_region(docs[0], gps_lat=10.0, gps_lon=10.0, **kwargs) is False
    # Non-Actual never enters the live set, whatever its geography.
    assert (
        doc_matches_region(
            _doc_with_status("Test"),
            province="ON",
            gps_lat=None,
            gps_lon=None,
            preferred_lang="en-CA",
        )
        is False
    )


# ---------------------------------------------------------------------------
# Area-group lifecycle — Alert_Location_Status (issue #45)
# ---------------------------------------------------------------------------

# ECCC segments one CAP document into an <info> block per (language × area
# group). The mixed fixture holds four: en/fr "active" over Calgary expiring
# +16 h, en/fr "ended" over Medicine Hat expiring +1 h. Points inside each ring:
_CALGARY = (52.0, -114.06)  # active group
_MEDICINE_HAT = (50.0, -112.06)  # ended group
_OTTAWA = (45.2, -75.7)  # neither


def _mixed_doc():
    (doc,) = _docs("eccc_cap_mixed_area_groups.xml")
    return doc


def test_location_status_prefers_11_over_10():
    """v1.1 wins when both layers are present, matching Alert_Name precedence."""
    info = CAPInfoDoc(
        parameters={
            "layer:EC-MSC-SMC:1.0:Alert_Location_Status": "active",
            "layer:EC-MSC-SMC:1.1:Alert_Location_Status": "ended",
        }
    )
    assert _location_status(info) == "ended"
    # Either layer alone is read…
    assert (
        _location_status(
            CAPInfoDoc(
                parameters={"layer:EC-MSC-SMC:1.0:Alert_Location_Status": "ended"}
            )
        )
        == "ended"
    )
    # …and a block with neither carries no signal.
    assert _location_status(CAPInfoDoc()) == ""


def test_is_terminal_info_fails_open_on_unknown_status():
    """Only the two known terminal tokens end an area group; anything else is live.

    11 of 92 documents in the 2026-07-22 national sample came from non-ECCC
    senders (Amber, flood, 911) with no such parameter at all — reading absence
    as terminal would silently drop them.
    """
    for status in ("", "active", "some_future_token"):
        info = CAPInfoDoc(
            parameters={"layer:EC-MSC-SMC:1.1:Alert_Location_Status": status}
            if status
            else {}
        )
        assert _is_terminal_info(info) is False, status
    for status in ("ended", "transitioned_out"):
        info = CAPInfoDoc(
            parameters={"layer:EC-MSC-SMC:1.1:Alert_Location_Status": status}
        )
        assert _is_terminal_info(info) is True, status


def test_select_region_info_gps_picks_users_area_group():
    """A GPS user inside the ended group gets *that* block, not infos[0].

    Core defect A: language-only selection always returned the first matching
    block, which is empirically the active one, so the entity read another area
    group's expires, severity and headline.
    """
    doc = _mixed_doc()
    lat, lon = _MEDICINE_HAT
    info = _select_region_info(
        doc, language="en-CA", province="", gps_lat=lat, gps_lon=lon
    )
    assert info is not None
    assert info is not doc.infos[0]
    assert info.headline == "yellow warning - air quality - ended"
    assert _is_terminal_info(info) is True


def test_select_region_info_prefers_active_when_both_match():
    """Province mode sees both groups; the alert is still live somewhere in AB."""
    doc = _mixed_doc()
    info = _select_region_info(
        doc, language="en-CA", province="AB", gps_lat=None, gps_lon=None
    )
    assert info is not None
    assert info.headline == "yellow warning - air quality - in effect"
    assert _is_terminal_info(info) is False


def test_select_region_info_returns_none_when_no_block_matches():
    """Out-of-region: the document does not concern this configuration."""
    doc = _mixed_doc()
    lat, lon = _OTTAWA
    assert (
        _select_region_info(
            doc, language="en-CA", province="", gps_lat=lat, gps_lon=lon
        )
        is None
    )
    assert (
        _select_region_info(
            doc, language="en-CA", province="ON", gps_lat=None, gps_lon=None
        )
        is None
    )


def test_select_region_info_single_info_unchanged():
    """One-area-group documents behave exactly as _select_info did."""
    (doc,) = _docs("eccc_cap_en_new_1.xml")
    only = doc.infos[0]
    assert (
        _select_region_info(
            doc, language="en-CA", province="ON", gps_lat=None, gps_lon=None
        )
        is only
    )
    # Unmatched language still falls back to the block that exists.
    assert (
        _select_region_info(
            doc, language="de-DE", province="ON", gps_lat=None, gps_lon=None
        )
        is only
    )


def test_all_ended_document_is_terminal():
    """Every block ended → a terminal alert, not a live "…- ended" entity.

    Core defect B: msgType stays Update and expires is an hour out, so the old
    code published an active entity whose headline read "ended".
    """
    from custom_components.cap_alerts.normalize import normalize_alerts

    docs = _docs("eccc_cap_all_ended.xml")
    alerts = build_alerts_from_cap_docs(
        docs, province="AB", gps_lat=None, gps_lon=None, preferred_lang="en-CA"
    )
    assert len(alerts) == 1
    assert alerts[0].lifecycle_status == "ended"
    (normalized,) = normalize_alerts(alerts)
    # "cancel" because expires is still an hour out: this alert was ended
    # early rather than run to completion (issue #95).
    assert normalized.phase == "cancel"


def test_mixed_area_groups_province_prefers_active():
    """False-all-clear guard: a mixed document must stay live in province mode.

    The province has no finer location than its SGC prefix, so "still in effect
    somewhere in AB" is the honest reading. Announcing an all-clear to users in
    the still-active part is the worst failure mode here.
    """
    from custom_components.cap_alerts.normalize import normalize_alerts

    alerts = build_alerts_from_cap_docs(
        [_mixed_doc()],
        province="AB",
        gps_lat=None,
        gps_lon=None,
        preferred_lang="en-CA",
    )
    assert len(alerts) == 1
    assert "in effect" in alerts[0].headline
    assert alerts[0].lifecycle_status == "active"
    (normalized,) = normalize_alerts(alerts)
    assert normalized.phase not in ("cancel", "expired")


def test_gps_inside_ended_group_yields_a_terminal_alert():
    """The #45 report, end to end: a user in the ended sub-area is released."""
    from custom_components.cap_alerts.normalize import normalize_alerts

    lat, lon = _MEDICINE_HAT
    alerts = build_alerts_from_cap_docs(
        [_mixed_doc()],
        province="",
        gps_lat=lat,
        gps_lon=lon,
        preferred_lang="en-CA",
    )
    assert len(alerts) == 1
    assert alerts[0].lifecycle_status == "ended"
    (normalized,) = normalize_alerts(alerts)
    # Ended early, so "cancel" rather than "expired" (issue #95).
    assert normalized.phase == "cancel"


def test_build_alerts_deduplicates_repeated_documents():
    """One Atom entry per (language × area group), all linking one CAP body.

    Defect C: the GeoRSS path hands the same document over up to four times.
    Left in, every copy resolved to the same <info> and the merge spliced the
    alert with itself — same language in headline and headline_alt.
    """
    doc = _mixed_doc()
    kwargs: dict[str, Any] = {
        "province": "AB",
        "gps_lat": None,
        "gps_lon": None,
        "preferred_lang": "en-CA",
    }
    once = build_alerts_from_cap_docs([doc], **kwargs)
    four_times = build_alerts_from_cap_docs([doc] * 4, **kwargs)

    assert len(four_times) == len(once) == 1
    assert four_times[0] == once[0]
    # Genuinely bilingual, not the same language spliced into both slots.
    assert four_times[0].language == "en-CA"
    assert four_times[0].language_alt == "fr-CA"
    assert "en vigueur" in four_times[0].headline_alt


def test_build_alerts_bilingual_primary_is_preferred_language():
    """The user's language wins, and the alternate carries the *other* one."""
    alerts = build_alerts_from_cap_docs(
        [_mixed_doc()],
        province="AB",
        gps_lat=None,
        gps_lon=None,
        preferred_lang="fr-CA",
    )
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.language == "fr-CA"
    assert alert.language_alt == "en-CA"
    assert "en vigueur" in alert.headline
    assert alert.headline_alt != alert.headline
    assert "in effect" in alert.headline_alt


@pytest.mark.parametrize("entry_language", ["en-CA", "fr-CA"])
def test_build_alerts_bilingual_ignores_atom_entry_language(entry_language: str):
    """Which entry landed last in the feed must not decide the primary language.

    ``atom_meta_by_id`` is keyed by identifier and is last-write-wins across an
    entry group, so the surviving language is an artefact of feed order. The CAP
    body carries both languages; ``preferred_lang`` is honoured against it.
    """
    doc = _mixed_doc()
    alerts = build_alerts_from_cap_docs(
        [doc],
        province="AB",
        gps_lat=None,
        gps_lon=None,
        preferred_lang="fr-CA",
        atom_meta_by_id={doc.identifier: {"language": entry_language}},
    )
    assert len(alerts) == 1
    assert alerts[0].language == "fr-CA"
    assert alerts[0].language_alt == "en-CA"


def test_doc_matches_region_matches_terminal_only_block():
    """Streaming-path guard: admission must keep the doc that ends the alert.

    The active group is out of region and only the ended group covers the user.
    Rejecting it here would mean the coordinator never learns the alert ended
    and the entity lingers — issue #45 on the streaming path.
    """
    lat, lon = _MEDICINE_HAT
    assert (
        doc_matches_region(
            _mixed_doc(),
            province="",
            gps_lat=lat,
            gps_lon=lon,
            preferred_lang="en-CA",
        )
        is True
    )


# ---------------------------------------------------------------------------
# Multi-host feed union (issue #38)
# ---------------------------------------------------------------------------

# An Ontario polygon shared by the union fixtures: envelope georss text is
# whitespace-separated "lat lon lat lon …"; the CAP-body form is comma-paired.
_UNION_ON_GEORSS = "45.0 -76.0 45.0 -75.5 45.5 -75.5 45.5 -76.0 45.0 -76.0"
_UNION_ON_CAP_POLY = "45.0,-76.0 45.0,-75.5 45.5,-75.5 45.5,-76.0 45.0,-76.0"


def _atom_feed(authority: str, entries: list[dict[str, str]]) -> str:
    """Build a NAAD-shaped Atom feed with live-shaped ``tag:<authority>`` ids.

    Each entry dict carries: ``oid`` (embedded in the Atom ``<id>`` as
    ``…/urn:oid:X``), ``cap_href``, ``polygon`` (georss text), and optional
    ``lang`` / ``status`` / ``title`` / ``web``.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:georss="http://www.georss.org/georss">',
        f"<id>tag:{authority},2026:feed.atom</id>",
    ]
    for e in entries:
        lang = e.get("lang", "en-CA")
        web = e.get("web", "https://www.weather.gc.ca/warnings/alert_en.html")
        parts += [
            "<entry>",
            f"<id>tag:{authority},2026:feed.atom/{e['oid']}</id>",
            f"<title>{e.get('title', 'Wind Warning in effect')}</title>",
            f'<link rel="alternate" type="application/cap+xml" href="{e["cap_href"]}"/>',
            f'<link rel="alternate" type="text/html" href="{web}"/>',
            f'<category term="status={e.get("status", "Actual")}"/>',
            '<category term="msgType=Alert"/>',
            f'<category term="language={lang}"/>',
            f"<georss:polygon>{e['polygon']}</georss:polygon>",
            "</entry>",
        ]
    parts.append("</feed>")
    return "".join(parts)


def _union_cap(
    identifier: str,
    *,
    sgc: str = "3506008",
    polygon: str = _UNION_ON_CAP_POLY,
    lang: str = "en-CA",
    event: str = "Wind Warning",
    event_code: str = "wind",
    headline: str = "Wind Warning in effect",
) -> str:
    """A minimal single-info Ontario CAP body for the union tests.

    ``event_code`` feeds the CAP-CP eventCode that ``_bilingual_key`` hashes, so
    distinct values yield distinct alert identities (same sender/sent/polygon
    would otherwise collapse into one).
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier>"
        "<sender>CWTO</sender><sent>2099-07-22T12:00:00-00:00</sent>"
        "<status>Actual</status><msgType>Alert</msgType><scope>Public</scope>"
        f"<info><language>{lang}</language><category>Met</category>"
        f"<event>{event}</event><severity>Moderate</severity>"
        f"<headline>{headline}</headline>"
        "<eventCode><valueName>profile:CAP-CP:Event:0.4</valueName>"
        f"<value>{event_code}</value></eventCode>"
        "<area><areaDesc>Somewhere</areaDesc>"
        f"<polygon>{polygon}</polygon>"
        "<geocode><valueName>profile:CAP-CP:Location:0.3</valueName>"
        f"<value>{sgc}</value></geocode>"
        "</area></info></alert>"
    )


def _atom_id_entry(atom_id: str) -> Element:
    entry = Element(f"{{{NS_ATOM}}}entry")
    SubElement(entry, f"{{{NS_ATOM}}}id").text = atom_id
    return entry


def test_resolve_feed_urls_auto_returns_both_in_order():
    both = [
        ("alertready", _eccc_mod.NAAD_FEED_ALERTREADY),
        ("pelmorex", _eccc_mod.NAAD_FEED_PELMOREX),
    ]
    assert _resolve_feed_urls({}) == both  # absent option → auto
    assert _resolve_feed_urls({"feed_source": "auto"}) == both


def test_resolve_feed_urls_named_source():
    assert _resolve_feed_urls({"feed_source": "pelmorex"}) == [
        ("pelmorex", _eccc_mod.NAAD_FEED_PELMOREX)
    ]
    assert _resolve_feed_urls({"feed_source": "alertready"}) == [
        ("alertready", _eccc_mod.NAAD_FEED_ALERTREADY)
    ]


def test_resolve_feed_urls_unrecognised_fails_open_to_both():
    assert _resolve_feed_urls({"feed_source": "garbage"}) == _resolve_feed_urls({})


def test_entry_oid_extracts_oid_across_hosts():
    oid = "urn:oid:2.49.0.1.124.abc.2026"
    ar = _atom_id_entry(f"tag:rsstrainingdqs.alertready.ca,2026:feed.atom/{oid}")
    pel = _atom_id_entry(f"tag:rss.naad-adna.pelmorex.com,2026:feed.atom/{oid}")
    assert _entry_oid(ar) == oid
    assert _entry_oid(ar) == _entry_oid(pel)


def test_entry_oid_fails_open_to_whole_id_without_oid():
    # The synthetic eccc_naad_atom.xml fixture carries no OID in its ids.
    atom_id = "https://www.naad-adna.pelmorex.com/uuid-en-new-1"
    assert _entry_oid(_atom_id_entry(atom_id)) == atom_id


@pytest.mark.asyncio
async def test_union_merges_hosts_deduplicated_by_oid():
    """auto unions both hosts; a shared OID is fetched once, from alertready."""
    shared, ar_only, pel_only = (
        "urn:oid:2.49.0.1.124.test.2026.SHARED",
        "urn:oid:2.49.0.1.124.test.2026.AR",
        "urn:oid:2.49.0.1.124.test.2026.PEL",
    )
    ar_shared = "https://cap.alertready.ca/shared.cap"
    pel_shared = "http://capcp2.naad-adna.pelmorex.com/shared.cap"
    ar_href = "https://cap.alertready.ca/ar_only.cap"
    pel_href = "http://capcp2.naad-adna.pelmorex.com/pel_only.cap"

    responses: dict[str, Any] = {
        _eccc_mod.NAAD_FEED_ALERTREADY: _atom_feed(
            "rsstrainingdqs.alertready.ca",
            [
                {"oid": shared, "cap_href": ar_shared, "polygon": _UNION_ON_GEORSS},
                {"oid": ar_only, "cap_href": ar_href, "polygon": _UNION_ON_GEORSS},
            ],
        ),
        _eccc_mod.NAAD_FEED_PELMOREX: _atom_feed(
            "rss.naad-adna.pelmorex.com",
            [
                {"oid": shared, "cap_href": pel_shared, "polygon": _UNION_ON_GEORSS},
                {"oid": pel_only, "cap_href": pel_href, "polygon": _UNION_ON_GEORSS},
            ],
        ),
        ar_shared: _union_cap("urn:oid:cap.SHARED", event_code="shared"),
        pel_shared: _union_cap("urn:oid:cap.SHARED", event_code="shared"),
        ar_href: _union_cap("urn:oid:cap.AR", event_code="aronly"),
        pel_href: _union_cap("urn:oid:cap.PEL", event_code="pelonly"),
    }
    session = StubSession(responses)
    alerts = await ECCCProvider().async_fetch(
        session, {"province": "ON"}, {"language": "en-CA"}
    )
    # shared + alertready-only + pelmorex-only = three distinct alerts.
    assert len(alerts) == 3
    # Decision 4: alertready's href wins the shared alert; the pelmorex (cleartext
    # HTTP) href for the same alert is never fetched (risk 2).
    assert ar_shared in session.requested
    assert pel_shared not in session.requested
    # Union still reaches the pelmorex-only alert.
    assert pel_href in session.requested


@pytest.mark.asyncio
async def test_union_tolerates_one_host_failing_and_warns_once(caplog):
    """One host down → alerts still come from the other, one warning per streak."""
    pel_href = "http://capcp2.naad-adna.pelmorex.com/pel.cap"
    pelmorex_feed = _atom_feed(
        "rss.naad-adna.pelmorex.com",
        [
            {
                "oid": "urn:oid:test.PEL",
                "cap_href": pel_href,
                "polygon": _UNION_ON_GEORSS,
            }
        ],
    )
    responses: dict[str, Any] = {
        _eccc_mod.NAAD_FEED_ALERTREADY: (503, ""),
        _eccc_mod.NAAD_FEED_PELMOREX: pelmorex_feed,
        pel_href: _union_cap("urn:oid:cap.PEL"),
    }
    provider = ECCCProvider()

    with caplog.at_level(logging.WARNING):
        alerts = await provider.async_fetch(
            StubSession(responses), {"province": "ON"}, {"language": "en-CA"}
        )
    assert len(alerts) == 1  # served by pelmorex despite alertready 503

    def _alertready_warnings() -> list:
        return [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "alertready" in r.getMessage()
        ]

    assert len(_alertready_warnings()) == 1

    # A second consecutive failure logs nothing further.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await provider.async_fetch(
            StubSession(responses), {"province": "ON"}, {"language": "en-CA"}
        )
    assert _alertready_warnings() == []

    # A success re-arms the warning for the next failure streak.
    ok = dict(responses)
    ok[_eccc_mod.NAAD_FEED_ALERTREADY] = _atom_feed("rsstrainingdqs.alertready.ca", [])
    await provider.async_fetch(
        StubSession(ok), {"province": "ON"}, {"language": "en-CA"}
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await provider.async_fetch(
            StubSession(responses), {"province": "ON"}, {"language": "en-CA"}
        )
    assert len(_alertready_warnings()) == 1


@pytest.mark.asyncio
async def test_union_all_hosts_failing_raises_naming_both():
    responses: dict[str, Any] = {
        _eccc_mod.NAAD_FEED_ALERTREADY: (503, ""),
        _eccc_mod.NAAD_FEED_PELMOREX: (500, ""),
    }
    with pytest.raises(_eccc_mod.UpdateFailed) as excinfo:
        await ECCCProvider().async_fetch(
            StubSession(responses), {"province": "ON"}, {"language": "en-CA"}
        )
    message = str(excinfo.value)
    assert "alertready" in message and "pelmorex" in message


@pytest.mark.asyncio
async def test_feed_source_override_fetches_only_pelmorex():
    pel_href = "http://capcp2.naad-adna.pelmorex.com/pel.cap"
    responses: dict[str, Any] = {
        _eccc_mod.NAAD_FEED_ALERTREADY: _atom_feed("rsstrainingdqs.alertready.ca", []),
        _eccc_mod.NAAD_FEED_PELMOREX: _atom_feed(
            "rss.naad-adna.pelmorex.com",
            [
                {
                    "oid": "urn:oid:test.P",
                    "cap_href": pel_href,
                    "polygon": _UNION_ON_GEORSS,
                }
            ],
        ),
        pel_href: _union_cap("urn:oid:cap.P"),
    }
    session = StubSession(responses)
    alerts = await ECCCProvider().async_fetch(
        session,
        {"province": "ON"},
        {"language": "en-CA", "feed_source": "pelmorex"},
    )
    assert len(alerts) == 1
    assert _eccc_mod.NAAD_FEED_ALERTREADY not in session.requested
    assert _eccc_mod.NAAD_FEED_PELMOREX in session.requested


@pytest.mark.asyncio
async def test_feed_source_alertready_parity_with_single_host_fixture():
    """feed_source=alertready reproduces the pre-union single-host result exactly."""
    responses: dict[str, Any] = {
        "https://rss.alertready.ca/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    alerts = await ECCCProvider().async_fetch(
        session,
        {"province": "ON"},
        {"language": "en-CA", "feed_source": "alertready"},
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Freezing Drizzle Advisory"
    # Pinned to one host: pelmorex is never contacted.
    assert _eccc_mod.NAAD_FEED_PELMOREX not in session.requested


@pytest.mark.asyncio
async def test_union_dedup_runs_after_region_filter():
    """Decision 3: dedup on survivors, not raw entries.

    A document with four entries (en/fr × active/ended) carries a *different*
    polygon per area group. The user's GPS sits in the ended group only, and the
    active entries sort first. Deduplicating raw entries by OID would keep the
    first (active) entry, whose polygon excludes the user, and drop the whole
    document — even though the user's own (ended) area group matched. Fails
    against a build that deduplicates before the region filter.
    """
    calgary = "51.8 -114.2 52.2 -114.2 52.0 -113.8 51.8 -114.2"  # active group
    med_hat = "49.8 -112.2 50.2 -112.2 50.0 -111.8 49.8 -112.2"  # ended group
    cap_href = "https://cap.alertready.ca/mixed.cap"
    oid = "urn:oid:2.49.0.1.124.test.2026.MIXED"
    feed = _atom_feed(
        "rsstrainingdqs.alertready.ca",
        [
            {"oid": oid, "cap_href": cap_href, "polygon": calgary, "lang": "en-CA"},
            {"oid": oid, "cap_href": cap_href, "polygon": calgary, "lang": "fr-CA"},
            {"oid": oid, "cap_href": cap_href, "polygon": med_hat, "lang": "en-CA"},
            {"oid": oid, "cap_href": cap_href, "polygon": med_hat, "lang": "fr-CA"},
        ],
    )
    responses: dict[str, Any] = {
        _eccc_mod.NAAD_FEED_ALERTREADY: feed,
        cap_href: _fixture("eccc_cap_mixed_area_groups.xml"),
    }
    lat, lon = _MEDICINE_HAT
    alerts = await ECCCProvider().async_fetch(
        StubSession(responses),
        {"gps_loc": f"{lat},{lon}"},
        {"language": "en-CA", "feed_source": "alertready"},
    )
    assert len(alerts) == 1
    assert alerts[0].lifecycle_status == "ended"
