"""Tests for the WMO SWIC provider — RSS → CAP XML two-step fetch."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

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
    # The real package, never a fabricated stand-in: ``providers/__init__.py``
    # is HA-free and defines ``AlertProvider``/``get_provider``, and a bare
    # ModuleType husk registered under this name shadows it for the rest of the
    # session — ``coordinator.py``'s ``from .providers import AlertProvider``
    # then fails in whichever file happens to import it next.
    importlib.import_module("cap_alerts.providers")
    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Ensure const and model are available before loading provider modules.
_load("const")
_load("model")
_cap_cache_mod = _load_provider("cap_content_cache")
_cap_mod = _load_provider("cap")  # wmo imports CAP parsing helpers from it
_wmo_mod = _load_provider("wmo")

CAPContentCache = _cap_cache_mod.CAPContentCache
WMOProvider = _wmo_mod.WMOProvider
_parse_rss_links = _wmo_mod._parse_rss_links
_compute_wmo_id = _wmo_mod._compute_wmo_id
_build_alert = _wmo_mod._build_alert
_language_matches = _wmo_mod._language_matches
_select_info = _wmo_mod._select_info
_select_alt_info = _wmo_mod._select_alt_info
_parse_cap_alert = _cap_mod.parse_cap_alert

from cap_alerts.const import (  # noqa: E402
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_SOURCE_ID,
)
from tests.conftest import StubSession  # noqa: E402 — after module setup


_RSS_URL = "https://severeweather.wmo.int/v2/cap-alerts/mx-smn-es/rss.xml"
_CAP_URL_1 = "https://swic.example/cap/MX-SMN-2026-001.xml"
_CAP_URL_2 = "https://swic.example/cap/MX-SMN-2026-002.xml"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _full_responses() -> dict[str, Any]:
    return {
        _RSS_URL: _fixture("wmo_rss.xml"),
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        _CAP_URL_2: _fixture("wmo_cap_2.xml"),
    }


# ---------------------------------------------------------------------------
# _parse_rss_links
# ---------------------------------------------------------------------------


def test_parse_rss_links_extracts_item_links():
    links = _parse_rss_links(_fixture("wmo_rss.xml"))
    assert links == [_CAP_URL_1, _CAP_URL_2]


def test_parse_rss_links_empty_feed_returns_empty():
    links = _parse_rss_links(
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<title>Empty</title></channel></rss>"
    )
    assert links == []


_CAP_RSS_NS = (
    '<?xml version="1.0"?>'
    '<rss version="2.0" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.1">'
    "<channel><title>Feed</title>"
    "<item><title>Expired</title><link>https://x/expired.xml</link>"
    "<cap:expires>Mon, 25 May 2026 09:00:00 +0000</cap:expires></item>"
    "<item><title>Live</title><link>https://x/live.xml</link>"
    "<cap:expires>Tue, 26 May 2026 09:00:00 +0000</cap:expires></item>"
    "<item><title>NoExpiry</title><link>https://x/noexp.xml</link></item>"
    "</channel></rss>"
)


def test_parse_rss_links_skips_expired_items():
    """Items whose cap:expires is in the past are dropped before CAP fetch."""
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    links = _parse_rss_links(_CAP_RSS_NS, now=now)
    # Expired item dropped; live + missing-expires (fail-open) kept.
    assert links == ["https://x/live.xml", "https://x/noexp.xml"]


def test_parse_rss_links_keeps_all_before_expiry():
    """When the cutoff precedes every expiry, all linked items are kept."""
    now = datetime(2026, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    links = _parse_rss_links(_CAP_RSS_NS, now=now)
    assert links == [
        "https://x/expired.xml",
        "https://x/live.xml",
        "https://x/noexp.xml",
    ]


# Thai (TMD) feeds emit Buddhist-Era years (Gregorian + 543) in the RSS
# envelope's RFC-2822 cap:expires. 2568 → 2025, 2600 → 2057.
_CAP_RSS_BE = (
    '<?xml version="1.0"?>'
    '<rss version="2.0" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.1">'
    "<channel><title>TMD</title>"
    "<item><title>Expired</title><link>https://x/be-expired.xml</link>"
    "<cap:expires>Mon, 25 May 2568 09:00:00 +0000</cap:expires></item>"
    "<item><title>Live</title><link>https://x/be-live.xml</link>"
    "<cap:expires>Tue, 26 May 2600 09:00:00 +0000</cap:expires></item>"
    "</channel></rss>"
)


def test_parse_rss_links_drops_expired_buddhist_era_item():
    """A BE expiry whose Gregorian date is past is dropped before CAP fetch."""
    # 2568 → 2025; cutoff 2026-05-25 is after the corrected 2025-05-25 expiry.
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    links = _parse_rss_links(_CAP_RSS_BE, now=now)
    assert links == ["https://x/be-live.xml"]


def test_parse_rss_links_keeps_live_buddhist_era_item():
    """A BE expiry whose Gregorian date is still future is kept."""
    # 2600 → 2057, comfortably after the cutoff.
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    links = _parse_rss_links(_CAP_RSS_BE, now=now)
    assert "https://x/be-live.xml" in links


# ---------------------------------------------------------------------------
# Provider flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_country_wide():
    session = StubSession(_full_responses())
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )

    assert len(alerts) == 2
    assert all(a.provider == "wmo" for a in alerts)

    by_event = {a.event: a for a in alerts}
    assert set(by_event) == {"Severe Thunderstorm Warning", "Flash Flood Warning"}

    storm = by_event["Severe Thunderstorm Warning"]
    assert storm.severity == "Severe"
    assert storm.identifier == "urn:wmo:mx-smn:2026:001"
    assert storm.id == _compute_wmo_id(storm.identifier, _CAP_URL_1)
    assert storm.area_desc == "Western Jalisco"
    assert storm.geometry is not None and storm.geometry["type"] == "Polygon"

    flood = by_event["Flash Flood Warning"]
    assert flood.severity == "Moderate"


@pytest.mark.asyncio
async def test_fetch_gps_inside_polygon():
    session = StubSession(_full_responses())
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es", CONF_GPS_LOC: "20.5,-104.0"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Severe Thunderstorm Warning"


@pytest.mark.asyncio
async def test_fetch_gps_outside_polygon():
    session = StubSession(_full_responses())
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es", CONF_GPS_LOC: "30.0,-90.0"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_rss_parse_error():
    session = StubSession({_RSS_URL: "this is not xml <<>>"})
    provider = WMOProvider()
    with pytest.raises(UpdateFailed):
        await provider.async_fetch(
            session,
            {CONF_SOURCE_ID: "mx-smn-es"},
            {},
            cap_content_cache=CAPContentCache(),
        )


@pytest.mark.asyncio
async def test_rss_non_200():
    session = StubSession({_RSS_URL: (503, "")})
    provider = WMOProvider()
    with pytest.raises(UpdateFailed):
        await provider.async_fetch(
            session,
            {CONF_SOURCE_ID: "mx-smn-es"},
            {},
            cap_content_cache=CAPContentCache(),
        )


@pytest.mark.asyncio
async def test_cap_fetch_failure_graceful():
    """One CAP URL 404s → that alert is skipped, the other still returns."""
    responses = {
        _RSS_URL: _fixture("wmo_rss.xml"),
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        # _CAP_URL_2 intentionally absent → StubSession returns 404 → None body.
    }
    session = StubSession(responses)
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Severe Thunderstorm Warning"


@pytest.mark.asyncio
async def test_revision_chain_resolution():
    """CAP 2 references CAP 1's identifier → only the leaf (CAP 2) survives."""
    cap_2_with_ref = _fixture("wmo_cap_2.xml").replace(
        "<scope>Public</scope>",
        "<scope>Public</scope>\n"
        "  <references>smn.conagua.gob.mx,urn:wmo:mx-smn:2026:001,"
        "2026-05-12T18:00:00-06:00</references>",
    )
    responses = {
        _RSS_URL: _fixture("wmo_rss.xml"),
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        _CAP_URL_2: cap_2_with_ref,
    }
    session = StubSession(responses)
    provider = WMOProvider()
    alerts = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].event == "Flash Flood Warning"
    assert alerts[0].identifier == "urn:wmo:mx-smn:2026:002"


@pytest.mark.asyncio
async def test_alert_identity_stable():
    """The same source over two polls yields the same alert id per identifier."""
    provider = WMOProvider()
    first = await provider.async_fetch(
        session := StubSession(_full_responses()),
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    second = await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: "mx-smn-es"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    ids_first = {a.identifier: a.id for a in first}
    ids_second = {a.identifier: a.id for a in second}
    assert ids_first == ids_second
    assert all(ids_first.values())


# ---------------------------------------------------------------------------
# _build_alert geocode container
# ---------------------------------------------------------------------------


def _cap_with_geocodes(geocodes: list[tuple[str, str]]) -> str:
    """Minimal CAP 1.2 doc carrying the given (valueName, value) area geocodes."""
    blocks = "\n".join(
        f"      <geocode><valueName>{scheme}</valueName>"
        f"<value>{value}</value></geocode>"
        for scheme, value in geocodes
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>WMO-TEST-1</identifier>
  <sender>test@wmo.int</sender>
  <sent>2026-07-29T12:00:00-00:00</sent>
  <status>Actual</status>
  <msgType>Alert</msgType>
  <scope>Public</scope>
  <info>
    <language>es-MX</language>
    <category>Met</category>
    <event>Aviso</event>
    <urgency>Expected</urgency>
    <severity>Moderate</severity>
    <certainty>Likely</certainty>
    <area>
      <areaDesc>Test Area</areaDesc>
{blocks}
    </area>
  </info>
</alert>
"""


def _alert_from_cap(xml: str) -> Any:
    doc = _parse_cap_alert(xml)
    assert doc is not None
    return _build_alert(doc, doc.infos[0], _CAP_URL_1, "test-id")


# ---------------------------------------------------------------------------
# Language selection (issue #59)
# ---------------------------------------------------------------------------


def _doc(name: str) -> Any:
    doc = _parse_cap_alert(_fixture(name))
    assert doc is not None
    return doc


def _multilang_doc(blocks: list[tuple[str, str]], *, polygon: bool = False) -> Any:
    """Parse a CAP doc carrying one ``<info>`` per ``(language, headline)``."""
    area = "      <areaDesc>Area</areaDesc>\n" + (
        "      <polygon>47.0,9.0 47.0,10.0 48.0,10.0 48.0,9.0 47.0,9.0</polygon>\n"
        if polygon
        else ""
    )
    infos = "\n".join(
        f"""  <info>
    <language>{language}</language>
    <category>Met</category>
    <event>{headline}</event>
    <urgency>Expected</urgency>
    <severity>Severe</severity>
    <certainty>Likely</certainty>
    <headline>{headline}</headline>
    <description>{headline} description</description>
    <area>
{area}    </area>
  </info>"""
        for language, headline in blocks
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>WMO-LANG-1</identifier>
  <sender>test@wmo.int</sender>
  <sent>2026-08-01T12:00:00-00:00</sent>
  <status>Actual</status>
  <msgType>Alert</msgType>
  <scope>Public</scope>
{infos}
</alert>
"""
    doc = _parse_cap_alert(xml)
    assert doc is not None
    return doc


# at-zamg-en: the source whose ID ends "-en" leads with a German block.
def _zamg_doc(*, polygon: bool = False) -> Any:
    return _multilang_doc(
        [("de-DE", "Sturmwarnung"), ("en-GB", "Storm warning")], polygon=polygon
    )


@pytest.mark.parametrize(
    ("info_lang", "preferred", "expected"),
    [
        ("zh-CN", "zh-Hans", True),
        ("en-GB", "en", True),
        ("en", "en-GB", True),
        ("EN-us", "en-US", True),
        ("en-EN", "en", True),
        ("zh-mo", "zh-Hans", True),
        ("sr-Latn", "sr", True),
        ("mk-MKD", "mk", True),
        ("de-DE", "en", False),
        ("", "en", False),
        ("en", "", False),
        ("TL", "en", False),
    ],
)
def test_language_matches(info_lang: str, preferred: str, expected: bool):
    assert _language_matches(info_lang, preferred) is expected


def test_select_info_prefers_requested_language():
    """Issue #59's reported case: zh-Hans on cn-cma-xx yields the zh-CN block."""
    info = _select_info(_doc("wmo_cap_multilang.xml"), "zh-Hans")
    assert info.language == "zh-CN"
    assert info.headline.startswith("蓬莱区气象台")


def test_select_info_picks_german_from_en_suffixed_source():
    """at-zamg-en leads with de-DE — a de user must not be served English."""
    info = _select_info(_zamg_doc(), "de")
    assert info.language == "de-DE"
    assert info.headline == "Sturmwarnung"


def test_select_info_falls_back_to_english():
    """A language the document lacks degrades to English, not to infos[0]."""
    info = _select_info(_doc("wmo_cap_multilang.xml"), "de")
    assert info.language == "en-US"


def test_select_info_falls_back_to_first_when_no_english():
    """No matching and no English block → document order (mo-smg-xx shape)."""
    info = _select_info(_doc("wmo_cap_multilang_no_en.xml"), "de")
    assert info.language == "zh-mo"


def test_select_info_without_language_is_unchanged():
    """An unset language option reproduces the pre-#59 infos[0] behavior."""
    doc = _doc("wmo_cap_multilang.xml")
    assert _select_info(doc, "") is doc.infos[0]


def test_select_info_single_block_never_regresses():
    """Single-language documents return their only block for any language."""
    doc = _doc("wmo_cap_1.xml")
    for language in ("", "es", "zh-Hans", "nonsense"):
        assert _select_info(doc, language) is doc.infos[0]


def test_select_info_blocks_without_language_tag():
    """Sources that omit <language> entirely still yield infos[0]."""
    doc = _multilang_doc([("", "First"), ("", "Second")])
    assert _select_info(doc, "de") is doc.infos[0]


def test_select_info_empty_infos_returns_blank():
    doc = _parse_cap_alert(
        '<?xml version="1.0"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>WMO-EMPTY</identifier><sender>t</sender>"
        "<sent>2026-08-01T12:00:00-00:00</sent><status>Actual</status>"
        "<msgType>Alert</msgType><scope>Public</scope></alert>"
    )
    assert doc is not None
    info = _select_info(doc, "en")
    assert info.language == "" and info.headline == ""


def test_select_info_duplicate_tags_first_match_wins():
    """ca-aema-xx repeats en-CA/fr-CA per area group; group 1 wins, as before."""
    doc = _multilang_doc(
        [
            ("en-CA", "Group 1 EN"),
            ("fr-CA", "Group 1 FR"),
            ("en-CA", "Group 2 EN"),
            ("fr-CA", "Group 2 FR"),
        ]
    )
    assert _select_info(doc, "en-CA").headline == "Group 1 EN"
    assert _select_info(doc, "en-CA") is doc.infos[0]


def test_select_alt_info_returns_other_block():
    doc = _zamg_doc()
    primary = _select_info(doc, "de")
    assert _select_alt_info(doc, primary) is doc.infos[1]


def test_select_alt_info_single_block_is_none():
    doc = _doc("wmo_cap_1.xml")
    assert _select_alt_info(doc, doc.infos[0]) is None


def test_build_alert_populates_alt_language_fields():
    doc = _doc("wmo_cap_multilang.xml")
    info = _select_info(doc, "zh-Hans")
    alert = _build_alert(doc, info, _CAP_URL_1, "test-id", _select_alt_info(doc, info))
    assert alert.language == "zh-CN"
    assert alert.language_alt == "en-US"
    assert alert.headline_alt.startswith("Penglai District Meteorological Observatory")
    # The en-US block carries an empty <instruction/>; "" must normalize to None.
    assert alert.instruction_alt is None
    # The English event is retained so icon dispatch has something matchable —
    # the presented zh-CN one is free text no keyword table can classify (#91).
    assert alert.event == "高温"
    assert alert.event_alt == "high temperature"


def test_build_alert_single_block_omits_alt_attributes():
    alert = _alert_from_cap(_fixture("wmo_cap_1.xml"))
    assert alert.language_alt == "" and alert.headline_alt == ""
    assert alert.event_alt == ""
    attrs = alert.to_attributes()
    assert "language_alt" not in attrs and "headline_alt" not in attrs
    assert "event_alt" not in attrs


def test_language_choice_does_not_change_geometry_or_severity():
    """Guards the GPS filter: only text differs between language blocks."""
    doc = _zamg_doc(polygon=True)
    built = []
    for language in ("de", "en"):
        info = _select_info(doc, language)
        built.append(
            _build_alert(doc, info, _CAP_URL_1, "test-id", _select_alt_info(doc, info))
        )
    de_alert, en_alert = built
    assert de_alert.headline != en_alert.headline
    assert de_alert.geometry == en_alert.geometry
    assert de_alert.geometry is not None
    assert de_alert.severity == en_alert.severity


_MULTILANG_RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>CMA</title>'
    f"<item><title>High temperature</title><link>{_CAP_URL_1}</link></item>"
    "</channel></rss>"
)


@pytest.mark.asyncio
async def test_fetch_honours_language_option():
    session = StubSession(
        {
            "https://severeweather.wmo.int/v2/cap-alerts/cn-cma-xx/rss.xml": (
                _MULTILANG_RSS
            ),
            _CAP_URL_1: _fixture("wmo_cap_multilang.xml"),
        }
    )
    alerts = await WMOProvider().async_fetch(
        session,
        {CONF_SOURCE_ID: "cn-cma-xx"},
        {CONF_LANGUAGE: "zh-Hans"},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].language == "zh-CN"
    assert alerts[0].event == "高温"
    assert alerts[0].language_alt == "en-US"


@pytest.mark.asyncio
async def test_fetch_without_language_option_is_unchanged():
    session = StubSession(
        {
            "https://severeweather.wmo.int/v2/cap-alerts/cn-cma-xx/rss.xml": (
                _MULTILANG_RSS
            ),
            _CAP_URL_1: _fixture("wmo_cap_multilang.xml"),
        }
    )
    alerts = await WMOProvider().async_fetch(
        session,
        {CONF_SOURCE_ID: "cn-cma-xx"},
        {},
        cap_content_cache=CAPContentCache(),
    )
    assert len(alerts) == 1
    assert alerts[0].language == "en-US"
    assert alerts[0].event == "high temperature"


def test_build_alert_surfaces_non_same_geocode_schemes():
    # WMO's sources are heterogeneous; a national scheme used to be dropped
    # because only SAME was read.
    alert = _alert_from_cap(
        _cap_with_geocodes([("SMN-MX:Estado", "09"), ("SAME", "012345")])
    )
    assert alert.geocodes == {"SMN-MX:Estado": ("09",), "SAME": ("012345",)}
    assert alert.to_attributes()["geocodes"]["SMN-MX:Estado"] == ["09"]


def test_build_alert_same_scheme_still_promoted():
    alert = _alert_from_cap(_cap_with_geocodes([("SAME", "012345")]))
    assert alert.geocode_same == ("012345",)
    assert alert.to_attributes()["geocode_same"] == ["012345"]


def test_build_alert_without_geocodes_leaves_container_empty():
    alert = _alert_from_cap(_cap_with_geocodes([]))
    assert alert.geocodes == {}
    assert alert.geocode_same == ()
