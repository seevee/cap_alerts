"""Tests for the Australian CAP-AU provider — EDXL-wrapped state feeds (#127).

Fixtures are trimmed live captures from 2026-09-19, two or three alerts per
feed, polygons thinned to a dozen vertices: NSW RFS (default-namespace CAP,
``<incidents>``, HTML in ``description``), Queensland (``cap:`` prefix,
0.5 km marker circles), WA (``cap:`` prefix, no ``AlertLevel`` parameter,
``expires == sent``, an empty ``<polygon/>``) and TasALERT (a 10 km circle
alongside real polygons, ``<incidents>`` with an agency prefix).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.cap_alerts.const import (
    AU_FEEDS,
    CONF_ALERT_LEVEL,
    CONF_PROVINCE,
)
from custom_components.cap_alerts.conventions import (
    au_alert_level,
    au_alert_level_severity,
    conventions_for,
)
from custom_components.cap_alerts.model import CAPAlert
from custom_components.cap_alerts.normalize import normalize_alerts
from custom_components.cap_alerts.providers import au as _au_mod
from custom_components.cap_alerts.providers.cap import (
    cap_doc_from_element,
    parse_cap_alert,
    select_info,
)
from tests.conftest import StubSession

_FIXTURES = Path(__file__).parent / "fixtures"

AUProvider = _au_mod.AUProvider
html_to_text = _au_mod.html_to_text
edxl_alert_elements = _au_mod.edxl_alert_elements
compute_au_id = _au_mod.compute_au_id
alert_level_rank = _au_mod.alert_level_rank
apply_alert_level_floor = _au_mod.apply_alert_level_floor


def _fixture(state: str) -> str:
    return (_FIXTURES / f"au_{state.lower()}.xml").read_text(encoding="utf-8")


def _url(state: str) -> str:
    return AU_FEEDS[state][1]


async def _fetch(
    state: str,
    body: Any = None,
    options: dict[str, Any] | None = None,
) -> tuple[list[CAPAlert], StubSession]:
    session = StubSession({_url(state): _fixture(state) if body is None else body})
    alerts = await AUProvider().async_fetch(
        session,  # type: ignore[arg-type]
        {CONF_PROVINCE: state},
        options or {},
        user_agent="test",
    )
    return alerts, session


def _by_headline(alerts: list[CAPAlert], needle: str) -> CAPAlert:
    return next(a for a in alerts if needle in a.headline)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_edxl_unwraps_default_namespace_alerts():
    elements = edxl_alert_elements(_fixture("NSW"))
    assert elements is not None
    assert len(elements) == 3
    doc = cap_doc_from_element(elements[0])
    assert doc.identifier == "2026-09-20T09:05:00.0000000:678279"
    assert doc.incidents == "678279"


def test_edxl_unwraps_prefixed_alerts_identically():
    """QLD writes ``<cap:alert>``; the namespace is read off the element."""
    elements = edxl_alert_elements(_fixture("QLD"))
    assert elements is not None
    docs = [cap_doc_from_element(el) for el in elements]
    assert [d.identifier for d in docs] == ["WARN-633", "QF7-26-110229"]
    assert docs[0].infos[0].parameters["AlertLevel"] == "Advice"
    assert docs[0].infos[0].circles == [(153.25640000486902, -27.879572887196982, 0.5)]


def test_edxl_empty_envelope_is_an_empty_list_not_a_failure():
    quiet = (
        '<?xml version="1.0"?>'
        '<EDXLDistribution xmlns="urn:oasis:names:tc:emergency:EDXL:DE:1.0">'
        "<distributionID>x</distributionID></EDXLDistribution>"
    )
    assert edxl_alert_elements(quiet) == []


def test_edxl_malformed_xml_is_none():
    assert edxl_alert_elements("<EDXLDistribution><contentObject>") is None


def test_edxl_accepts_a_bare_cap_document():
    """The CAP namespace is what is matched, not the envelope."""
    elements = edxl_alert_elements(
        (_FIXTURES / "cap_circle_point.xml").read_text(encoding="utf-8")
    )
    assert elements is not None
    assert len(elements) == 1


def test_incidents_is_parsed_by_the_text_entry_point_too():
    doc = parse_cap_alert(
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>a</identifier><incidents>SES:IDT1</incidents></alert>"
    )
    assert doc is not None
    assert doc.incidents == "SES:IDT1"


# ---------------------------------------------------------------------------
# Text flattening
# ---------------------------------------------------------------------------


def test_html_to_text_flattens_nsw_description():
    text = html_to_text(
        "ALERT LEVEL: Advice<br />LOCATION: ABUNDANCE RD, MEDOWIE 2318<br />"
        "STATUS: Being controlled<a href='https://example' target='_blank'>"
        " More information</a> "
    )
    assert text == (
        "ALERT LEVEL: Advice\nLOCATION: ABUNDANCE RD, MEDOWIE 2318\n"
        "STATUS: Being controlled More information"
    )


def test_html_to_text_unescapes_and_collapses_blank_runs():
    assert html_to_text("a &amp; b<br><br><br><br>c") == "a & b\n\nc"


def test_html_to_text_leaves_plain_text_alone():
    plain = "A fire is burning off Wongawallan Drive.\n\nStay informed."
    assert html_to_text(plain) == plain
    assert html_to_text("") == ""


# ---------------------------------------------------------------------------
# Identity, tiers
# ---------------------------------------------------------------------------


def test_identity_prefers_the_incident_reference():
    elements = edxl_alert_elements(_fixture("NSW"))
    assert elements is not None
    doc = cap_doc_from_element(elements[0])
    info = select_info(doc, "")
    # A re-minted identifier on the next update must not change the id.
    doc.identifier = "2026-09-20T10:00:00.0000000:678279"
    assert compute_au_id("NSW", doc, info) == compute_au_id(
        "NSW", cap_doc_from_element(elements[0]), info
    )


def test_identity_falls_back_to_the_identifier_and_is_state_scoped():
    elements = edxl_alert_elements(_fixture("QLD"))
    assert elements is not None
    doc = cap_doc_from_element(elements[0])
    info = select_info(doc, "")
    assert doc.incidents == ""
    assert compute_au_id("QLD", doc, info) != compute_au_id("NSW", doc, info)
    assert len(compute_au_id("QLD", doc, info)) == 12


def test_identity_separates_two_products_of_one_incident():
    """TAS: a Bushfire Advice and a Smoke Alert share ``<incidents>`` (#218)."""
    elements = edxl_alert_elements(_fixture("TAS_TWO_PRODUCTS"))
    assert elements is not None
    advice, smoke = (cap_doc_from_element(el) for el in elements)
    assert advice.incidents == smoke.incidents == "TFS:036999-20092026"
    advice_info, smoke_info = select_info(advice, ""), select_info(smoke, "")
    assert list(advice_info.event_codes.values()) == ["bushFire"]
    assert list(smoke_info.event_codes.values()) == ["smoke"]
    assert compute_au_id("TAS", advice, advice_info) != compute_au_id(
        "TAS", smoke, smoke_info
    )
    # The product's re-issue under a fresh counter keeps its id.
    smoke.identifier = "036999-20092026-83600"
    assert compute_au_id("TAS", smoke, smoke_info) == compute_au_id(
        "TAS", cap_doc_from_element(elements[1]), smoke_info
    )


def test_identity_without_an_event_code_still_keys_on_the_incident():
    """A feed that drops the code (WA's is malformed) degrades to the old key."""
    elements = edxl_alert_elements(_fixture("NSW"))
    assert elements is not None
    doc = cap_doc_from_element(elements[0])
    info = select_info(doc, "")
    bare = replace(info, event_codes={})
    assert compute_au_id("NSW", doc, bare) != compute_au_id("NSW", doc, info)
    assert len(compute_au_id("NSW", doc, bare)) == 12


@pytest.mark.parametrize(
    ("level", "rank"),
    [
        ("Advice", 1),
        ("advice", 1),
        ("Watch and Act", 2),
        ("Emergency Warning", 3),
        ("Information", 0),
        ("Not Applicable", 0),
        ("Planned Burn", 0),
        ("", 0),
    ],
)
def test_alert_level_rank(level, rank):
    assert alert_level_rank(level) == rank


def test_alert_level_reads_the_parameter_verbatim():
    assert au_alert_level({"AlertLevel": "Planned Burn"}, "Bushfire Advice x") == (
        "Planned Burn"
    )


@pytest.mark.parametrize(
    ("headline", "level"),
    [
        ("Bushfire Advice MONITOR CONDITIONS - LAKE ARGYLE", "Advice"),
        ("Bushfire Watch and Act - PREPARE TO LEAVE", "Watch and Act"),
        ("BUSHFIRE EMERGENCY WARNING - LEAVE NOW", "Emergency Warning"),
        ("Facility or Park Closure", ""),
    ],
)
def test_alert_level_falls_back_to_the_headline(headline, level):
    assert au_alert_level(None, headline) == level
    assert au_alert_level({"Status": "Actual"}, headline) == level


@pytest.mark.parametrize(
    ("parameters", "headline", "severity"),
    [
        ({"AlertLevel": "Emergency Warning"}, "", "extreme"),
        ({"AlertLevel": "Watch and Act"}, "", "severe"),
        ({"AlertLevel": "Advice"}, "", "moderate"),
        ({"AlertLevel": "Information"}, "", "minor"),
        ({"AlertLevel": "Not Applicable"}, "", "minor"),
        ({"AlertLevel": "Planned Burn"}, "", "minor"),
        (None, "Bushfire Watch and Act - x", "severe"),
        (None, "Facility or Park Closure", None),
        ({"AlertLevel": "Something New"}, "", None),
    ],
)
def test_alert_level_severity(parameters, headline, severity):
    alert = CAPAlert(id="x", parameters=parameters, headline=headline, provider="au")
    assert au_alert_level_severity(alert) == severity


def test_convention_row_uses_the_tier_for_severity():
    assert conventions_for("au").severity is au_alert_level_severity


# ---------------------------------------------------------------------------
# Fetch: NSW
# ---------------------------------------------------------------------------


async def test_nsw_builds_one_alert_per_content_object():
    alerts, session = await _fetch("NSW")
    assert session.requested == [_url("NSW")]
    assert len(alerts) == 3
    assert {a.provider for a in alerts} == {"au"}
    assert all(a.url == _url("NSW") for a in alerts)


async def test_nsw_alert_fields():
    alerts, _ = await _fetch("NSW")
    alert = _by_headline(alerts, "ABUNDANCE RD")
    assert alert.identifier == "2026-09-20T09:05:00.0000000:678279"
    assert alert.event == "Bushfire"
    assert alert.category == "Fire"
    assert alert.sender_name == "NSW Rural Fire Service"
    assert alert.language == "en-AU"
    assert alert.sent == "2026-09-20T09:05:00+10:00"
    assert alert.effective == "2026-09-20T09:05:00+10:00"
    assert alert.geocodes == {
        "urn:oasis:names:tc:emergency:cap:1.2:profile:CAP-AU:1.0:ISO3166-2": ("AU-NSW",)
    }
    assert alert.parameters is not None
    assert alert.parameters["AlertLevel"] == "Advice"
    assert alert.parameters["CouncilArea"] == "Port Stephens"
    # Empty-valued parameters are not published.
    assert "Evacuation" not in alert.parameters
    assert "AllocatedResources" not in alert.parameters


async def test_nsw_description_is_flattened():
    alerts, _ = await _fetch("NSW")
    alert = _by_headline(alerts, "ABUNDANCE RD")
    assert "<br" not in alert.description
    assert "<a " not in alert.description
    assert alert.description.startswith("ALERT LEVEL: Advice\nLOCATION:")
    assert alert.instruction == (
        "A fire has started There is no immediate danger. "
        "Stay up to date in case the situation changes"
    )


async def test_expires_is_never_carried():
    """A regeneration TTL is not an end time (issue #127, gap 4)."""
    for state in ("NSW", "QLD", "WA", "TAS"):
        alerts, _ = await _fetch(state)
        assert alerts, state
        assert all(a.expires == "" for a in alerts), state


async def test_nsw_polygon_alert_keeps_both_shapes():
    alerts, _ = await _fetch("NSW")
    alert = _by_headline(alerts, "ABUNDANCE RD")
    assert alert.geometry is not None
    assert alert.geometry["type"] == "MultiPolygon"
    assert len(alert.geometry["coordinates"]) == 4
    assert alert.points == ((151.859176636, -32.758590698),)


async def test_nsw_point_only_alert_is_point_shaped():
    alerts, _ = await _fetch("NSW")
    alert = _by_headline(alerts, "CITRIS DR")
    assert alert.geometry == {
        "type": "Point",
        "coordinates": [153.007487001, -29.904569001],
    }
    assert alert.points == ((153.007487001, -29.904569001),)


async def test_nsw_identity_survives_a_re_minted_identifier():
    alerts, _ = await _fetch("NSW")
    first = _by_headline(alerts, "ABUNDANCE RD")
    body = _fixture("NSW").replace(
        "2026-09-20T09:05:00.0000000:678279", "2026-09-20T10:00:00.0000000:678279"
    )
    alerts, _ = await _fetch("NSW", body=body)
    assert _by_headline(alerts, "ABUNDANCE RD").id == first.id


async def test_normalized_severity_follows_the_tier():
    alerts, _ = await _fetch("NSW")
    normalized = {a.headline: a for a in normalize_alerts(alerts)}
    assert normalized["ABUNDANCE RD, MEDOWIE"].severity_normalized == "moderate"
    assert normalized["CITRIS DR, WELLS CROSSING"].severity_normalized == "minor"
    assert normalized["ARTHURS FOREST RD, COOPLACURRIPA"].severity_normalized == (
        "minor"
    )
    # No expiry means no expiry-driven phase.
    assert {a.phase for a in normalized.values()} == {"new"}


# ---------------------------------------------------------------------------
# Fetch: QLD, WA, TAS
# ---------------------------------------------------------------------------


async def test_qld_marker_circles_become_points():
    alerts, _ = await _fetch("QLD")
    assert len(alerts) == 2
    wong = _by_headline(alerts, "Wongawallan")
    assert wong.geometry is not None
    assert wong.geometry["type"] == "Polygon"
    assert wong.points == ((153.25640000486902, -27.879572887196982),)
    iron = _by_headline(alerts, "IRON RANGE")
    assert iron.geometry is not None
    assert iron.geometry["type"] == "Point"
    assert iron.parameters is not None
    assert iron.parameters["AlertLevel"] == "Information"


async def test_qld_instruction_is_flattened():
    alerts, _ = await _fetch("QLD")
    wong = _by_headline(alerts, "Wongawallan")
    assert wong.instruction is not None
    assert "<a " not in wong.instruction
    assert "Click here for current QFD incidents and warnings" in wong.instruction


async def test_wa_fills_in_the_alert_level_from_the_headline():
    alerts, _ = await _fetch("WA")
    assert len(alerts) == 2
    advice = _by_headline(alerts, "LAKE ARGYLE")
    assert advice.parameters is not None
    assert advice.parameters["AlertLevel"] == "Advice"
    assert advice.parameters["DFES_Region"] == "KIMBERLEY"
    # Trailing spaces on the wire are not carried.
    assert advice.urgency == "Future"
    assert advice.severity == "Minor"
    assert advice.geometry is not None
    assert advice.geometry["type"] == "Polygon"
    assert advice.points == ((128.84716011405118, -16.03406709103146),)


async def test_wa_closure_has_no_tier_and_keeps_cap_severity():
    alerts, _ = await _fetch("WA")
    closure = _by_headline(alerts, "Closure")
    assert closure.parameters is not None
    assert "AlertLevel" not in closure.parameters
    # The empty <polygon/> is dropped; the marker is the geometry.
    assert closure.geometry is not None
    assert closure.geometry["type"] == "Point"
    normalized = normalize_alerts([closure])[0]
    assert normalized.severity_normalized == "minor"


async def test_tas_real_radius_circle_is_not_a_point():
    alerts, _ = await _fetch("TAS")
    assert len(alerts) == 1
    storm = alerts[0]
    assert storm.identifier == "IDT21037-83466"
    assert storm.event == "Storm"
    assert storm.category == "Met"
    assert storm.sender_name == "State Emergency Service"
    assert storm.points == ()
    assert storm.geometry is not None
    assert storm.geometry["type"] == "MultiPolygon"
    assert len(storm.geometry["coordinates"]) == 7
    assert storm.parameters is not None
    assert storm.parameters["AlertLevel"] == "Advice"
    assert storm.parameters["Agency"] == "State Emergency Service"


async def test_tas_identity_uses_the_prefixed_incident():
    alerts, _ = await _fetch("TAS")
    elements = edxl_alert_elements(_fixture("TAS"))
    assert elements is not None
    doc = cap_doc_from_element(elements[0])
    assert doc.incidents == "SES:IDT21037"
    assert alerts[0].id == compute_au_id("TAS", doc, select_info(doc, ""))


async def test_tas_two_products_of_one_incident_are_two_alerts():
    """Live shape of 2026-09-20: the Advice was hidden behind the Smoke Alert (#218)."""
    alerts, _ = await _fetch("TAS", body=_fixture("TAS_TWO_PRODUCTS"))
    assert len(alerts) == 2
    assert len({a.id for a in alerts}) == 2
    advice = _by_headline(alerts, "Bushfire Advice - Seven Mile Beach")
    smoke = _by_headline(alerts, "Bushfire Smoke Alert - Dodges Ferry")
    assert advice.parameters is not None and smoke.parameters is not None
    assert advice.parameters["AlertLevel"] == "Advice"
    assert smoke.parameters["AlertLevel"] == "Not Yet Known"


# ---------------------------------------------------------------------------
# Minimum alert level
# ---------------------------------------------------------------------------


async def test_floor_all_keeps_everything():
    alerts, _ = await _fetch("NSW", options={CONF_ALERT_LEVEL: "All"})
    assert len(alerts) == 3


async def test_floor_advice_drops_the_informational_tiers():
    alerts, _ = await _fetch("NSW", options={CONF_ALERT_LEVEL: "Advice"})
    assert [a.headline for a in alerts] == ["ABUNDANCE RD, MEDOWIE"]


async def test_floor_watch_and_act_drops_advice():
    alerts, _ = await _fetch("NSW", options={CONF_ALERT_LEVEL: "Watch and Act"})
    assert alerts == []


async def test_floor_applies_to_a_headline_derived_tier():
    alerts, _ = await _fetch("WA", options={CONF_ALERT_LEVEL: "Advice"})
    assert [a.headline for a in alerts] == [
        "Bushfire Advice MONITOR CONDITIONS - LAKE ARGYLE"
    ]


def test_unknown_floor_applies_nothing():
    alerts = [CAPAlert(id="x", headline="Bushfire Advice - y", provider="au")]
    assert apply_alert_level_floor(alerts, "Purple") == alerts
    assert apply_alert_level_floor(alerts, "") == alerts


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_http_error_raises_rather_than_ending_every_incident():
    with pytest.raises(UpdateFailed, match="HTTP 503"):
        await _fetch("NSW", body=(503, ""))


async def test_malformed_feed_raises():
    with pytest.raises(UpdateFailed, match="not well-formed"):
        await _fetch("QLD", body="<EDXLDistribution><contentObject>")


async def test_quiet_envelope_is_an_empty_poll():
    quiet = (
        '<?xml version="1.0"?>'
        '<EDXLDistribution xmlns="urn:oasis:names:tc:emergency:EDXL:DE:1.0">'
        "<distributionID>TFSUniqueID</distributionID></EDXLDistribution>"
    )
    alerts, _ = await _fetch("TAS", body=quiet)
    assert alerts == []


async def test_unknown_state_raises():
    session = StubSession({})
    with pytest.raises(UpdateFailed, match="unknown state"):
        await AUProvider().async_fetch(
            session,  # type: ignore[arg-type]
            {CONF_PROVINCE: "VIC"},
            {},
            user_agent="test",
        )
    assert session.requested == []


async def test_alert_without_identifier_is_skipped():
    body = _fixture("QLD").replace(
        "<cap:identifier>WARN-633</cap:identifier>", "<cap:identifier/>"
    )
    alerts, _ = await _fetch("QLD", body=body)
    assert [a.identifier for a in alerts] == ["QF7-26-110229"]


async def test_user_agent_is_sent():
    _, session = await _fetch("WA")
    assert session.request_headers == [{"User-Agent": "test"}]
