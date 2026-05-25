"""Tests for ECCC provider — CAP-body parity (description, timestamps, lifecycle)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"
_FIXTURES = Path(__file__).parent / "fixtures"


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


# Ensure const and model are available before loading provider modules
_load("const")
_load("model")
_cap_cache_mod = _load_provider("cap_content_cache")
_cap_mod = _load_provider("cap")  # shared CAP parsing, used by eccc + wmo
_eccc_mod = _load_provider("eccc")

CAPContentCache = _cap_cache_mod.CAPContentCache
ECCCProvider = _eccc_mod.ECCCProvider
_pick_cap_link = _eccc_mod._pick_cap_link
_parse_cap_alert = _cap_mod.parse_cap_alert
_select_info = _eccc_mod._select_info
_resolve_chain_leaves = _cap_mod.resolve_chain_leaves
_bilingual_key = _eccc_mod._bilingual_key
_fallback_id = _eccc_mod._fallback_id
_build_alert_from_cap = _eccc_mod._build_alert_from_cap
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
        "https://rss.naad-adna.pelmorex.com/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    cache = CAPContentCache()
    config = {"province": "ON"}
    options = {"language": "en-CA"}

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


@pytest.mark.asyncio
async def test_eccc_provider_filters_expired_alert():
    """CAP expires in the past → normalize tags phase=expired."""
    from cap_alerts.normalize import normalize_alerts

    # Create a CAP file with past expires
    xml_expired = _fixture("eccc_cap_en_update_1.xml").replace(
        "<expires>2026-01-15T20:00:00-05:00</expires>",
        "<expires>2020-01-01T00:00:00+00:00</expires>",
    )
    responses: dict[str, Any] = {
        "https://rss.naad-adna.pelmorex.com/": _atom_xml(),
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
        session, {"province": "ON"}, {"language": "en-CA"}
    )

    normalized = normalize_alerts(alerts)
    assert len(normalized) == 1
    assert normalized[0].phase == "expired"


@pytest.mark.asyncio
async def test_eccc_provider_metadata_only_fallback_on_fetch_failure():
    """CAP fetch returns 404 → alert surfaces with Atom-only fields, empty long-form text."""
    responses: dict[str, Any] = {
        "https://rss.naad-adna.pelmorex.com/": _atom_xml(),
        # All CAP files return 404
        "https://cap.naad-adna.pelmorex.com/alerts/en_new_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_new_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/en_update_1.cap": (404, ""),
        "https://cap.naad-adna.pelmorex.com/alerts/fr_update_1.cap": (404, ""),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA"}
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
        "https://rss.naad-adna.pelmorex.com/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA"}
    )
    # Test entry should not appear; only the ON freezing drizzle series
    assert all(alert.area_desc != "" for alert in alerts)
    # The test entry's event would be "test alert"
    assert not any("test alert" in (a.event or "").lower() for a in alerts)


@pytest.mark.asyncio
async def test_eccc_provider_filters_foreign_province():
    """BC entry is filtered out when province=ON."""
    responses: dict[str, Any] = {
        "https://rss.naad-adna.pelmorex.com/": _atom_xml(),
        **_cap_responses(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(
        session, {"province": "ON"}, {"language": "en-CA"}
    )
    assert not any("Vancouver" in (a.area_desc or "") for a in alerts)


@pytest.mark.asyncio
async def test_eccc_provider_returns_empty_when_no_location_configured():
    """Neither province nor GPS → empty list."""
    responses: dict[str, Any] = {
        "https://rss.naad-adna.pelmorex.com/": _atom_xml(),
    }
    session = StubSession(responses)
    provider = ECCCProvider()
    alerts = await provider.async_fetch(session, {}, {"language": "en-CA"})
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
    cache = CAPContentCache(max_entries=2)

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
