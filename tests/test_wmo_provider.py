"""Tests for the WMO SWIC provider — feed of links → CAP XML two-step fetch.

Two feeds can carry a source (issue #210): the SWIC mirror and the feed the
registry record names as the authority's own. ``feed_source`` ``auto`` unions
them; the tests below pin the union, the dedup, the per-feed failure
tolerance, the body-level filters the authority feed needs, and the two pins.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.cap_alerts.const import (
    CONF_FEED_SOURCE,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_SOURCE_ID,
    WMO_SOURCES_URL,
)
from custom_components.cap_alerts.providers import ProviderError
from custom_components.cap_alerts.providers import cap as _cap_mod
from custom_components.cap_alerts.providers import cap_content_cache as _cap_cache_mod
from custom_components.cap_alerts.providers import wmo as _wmo_mod
from tests.conftest import StubSession

_FIXTURES = Path(__file__).parent / "fixtures"


CAPContentCache = _cap_cache_mod.CAPContentCache
WMOProvider = _wmo_mod.WMOProvider
_parse_feed_links = _wmo_mod._parse_feed_links
_compute_wmo_id = _wmo_mod._compute_wmo_id
_build_alert = _wmo_mod._build_alert
_language_matches = _wmo_mod._language_matches
_select_info = _wmo_mod._select_info
_select_alt_info = _wmo_mod._select_alt_info
_parse_cap_alert = _cap_mod.parse_cap_alert


_RSS_URL = "https://severeweather.wmo.int/v2/cap-alerts/mx-smn-es/rss.xml"
# mx-smn-es's registry record names a national host; an empty feed there
# keeps the existing fetch tests on the mirror's alerts under ``auto``.
_MX_AUTHORITY_URL = "https://smn.conagua.gob.mx/tools/GUI/webservices/cap.xml"
_EMPTY_RSS = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
_CAP_URL_1 = "https://swic.example/cap/MX-SMN-2026-001.xml"
_CAP_URL_2 = "https://swic.example/cap/MX-SMN-2026-002.xml"

# tl-dnmg-en: a bucket-backed source, mirror and authority feeds both stubbed.
_TL_MIRROR_URL = "https://severeweather.wmo.int/v2/cap-alerts/tl-dnmg-en/rss.xml"
_TL_BUCKET_URL = "https://cap-sources.s3.amazonaws.com/tl-dnmg-en/rss.xml"
_TL_BUCKET_CAP_1 = (
    "https://cap-sources.s3.amazonaws.com/tl-dnmg-en/2026-05-12-18-00-00.xml"
)
_TL_BUCKET_CAP_3 = (
    "https://cap-sources.s3.amazonaws.com/tl-dnmg-en/2026-05-12-18-15-00.xml"
)
_TL_BUCKET_EXPIRED = (
    "https://cap-sources.s3.amazonaws.com/tl-dnmg-en/2026-05-10-09-00-00.xml"
)
_TL_BUCKET_TEST = (
    "https://cap-sources.s3.amazonaws.com/tl-dnmg-en/2026-05-05-12-16-22.xml"
)

# th-tmd-en: #302's source, a national host, captured 2026-10-12.
_TH_MIRROR_URL = "https://severeweather.wmo.int/v2/cap-alerts/th-tmd-en/rss.xml"
_TMD_FEED_URL = "https://www.tmd.go.th/en/api/xml/CAP"
_TMD_CAP_URL = "https://www.tmd.go.th/uploads/CAP/en/CAPTMD20261012165615_2.xml"
_TMD_CAPTURED_AT = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)

_ID_1 = "urn:wmo:mx-smn:2026:001"
_ID_2 = "urn:wmo:mx-smn:2026:002"
_ID_3 = "urn:wmo:mx-smn:2026:003"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _full_responses() -> dict[str, Any]:
    return {
        WMO_SOURCES_URL: _fixture("wmo_sources.json"),
        _RSS_URL: _fixture("wmo_rss.xml"),
        _MX_AUTHORITY_URL: _EMPTY_RSS,
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        _CAP_URL_2: _fixture("wmo_cap_2.xml"),
    }


def _union_responses() -> dict[str, Any]:
    """tl-dnmg-en with both feeds up: the mirror lists 001 and 002, the bucket
    lists 001 (same body, bucket URL), 003, an expired 004 and a Test body."""
    return {
        WMO_SOURCES_URL: _fixture("wmo_sources.json"),
        _TL_MIRROR_URL: _fixture("wmo_rss.xml"),
        _TL_BUCKET_URL: _fixture("wmo_bucket_rss.xml"),
        _CAP_URL_1: _fixture("wmo_cap_1.xml"),
        _CAP_URL_2: _fixture("wmo_cap_2.xml"),
        _TL_BUCKET_CAP_1: _fixture("wmo_cap_1.xml"),
        _TL_BUCKET_CAP_3: _fixture("wmo_cap_3.xml"),
        _TL_BUCKET_EXPIRED: _fixture("wmo_cap_expired.xml"),
        _TL_BUCKET_TEST: _fixture("wmo_cap_test_status.xml"),
    }


async def _fetch(
    provider: WMOProvider,
    session: StubSession,
    source_id: str,
    options: dict[str, Any] | None = None,
    **config: Any,
) -> list[Any]:
    return await provider.async_fetch(
        session,
        {CONF_SOURCE_ID: source_id, **config},
        options or {},
        cap_content_cache=CAPContentCache(),
    )


def _pin_clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    """Freeze the provider's poll clock over a captured feed."""
    monkeypatch.setattr(_wmo_mod, "_utcnow", lambda: when)


# ---------------------------------------------------------------------------
# _parse_feed_links
# ---------------------------------------------------------------------------


def test_parse_feed_links_extracts_item_links():
    links = _parse_feed_links(_fixture("wmo_rss.xml"))
    assert links == [_CAP_URL_1, _CAP_URL_2]


def test_parse_feed_links_rss_without_cap_extensions_keeps_every_item():
    """The bucket feed carries no cap:expires, so nothing is pre-dropped."""
    links = _parse_feed_links(_fixture("wmo_bucket_rss.xml"))
    assert links == [
        _TL_BUCKET_CAP_1,
        _TL_BUCKET_CAP_3,
        _TL_BUCKET_EXPIRED,
        _TL_BUCKET_TEST,
    ]


def test_parse_feed_links_atom_href():
    """DWD-shaped Atom: the URL is a link href, and the cap+xml link wins."""
    links = _parse_feed_links(_fixture("wmo_atom_feed.xml"))
    assert len(links) == 2
    assert all(
        link.startswith("https://www.dwd.de/DWD/warnungen/cap-feed/de/")
        for link in links
    )
    assert links[0].endswith("9e2b5f3c-8356-4d50-b874-850dacee8d6e.DEU.xml")


def test_parse_feed_links_atom_entry_without_href_is_skipped():
    atom = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>no link</title></entry>"
        '<entry><link rel="self" href="https://x/self"/>'
        '<link href="https://x/untyped.xml"/></entry>'
        '<entry><link rel="alternate" href="https://x/html"/>'
        '<link rel="related" type="application/cap+xml" href="https://x/cap.xml"/></entry>'
        "</feed>"
    )
    assert _parse_feed_links(atom) == ["https://x/untyped.xml", "https://x/cap.xml"]


def test_parse_feed_links_tmd_capture():
    """Thailand's own feed (#302) is RSS of CAP links; read unchanged."""
    links = _parse_feed_links(_fixture("wmo_tmd_rss.xml"), now=_TMD_CAPTURED_AT)
    assert links[0] == _TMD_CAP_URL
    assert len(links) == 3


def test_parse_feed_links_empty_feed_returns_empty():
    links = _parse_feed_links(
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


def test_parse_feed_links_skips_expired_items():
    """Items whose cap:expires is in the past are dropped before CAP fetch."""
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    links = _parse_feed_links(_CAP_RSS_NS, now=now)
    # Expired item dropped; live + missing-expires (fail-open) kept.
    assert links == ["https://x/live.xml", "https://x/noexp.xml"]


def test_parse_feed_links_keeps_all_before_expiry():
    """When the cutoff precedes every expiry, all linked items are kept."""
    now = datetime(2026, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    links = _parse_feed_links(_CAP_RSS_NS, now=now)
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


def test_parse_feed_links_drops_expired_buddhist_era_item():
    """A BE expiry whose Gregorian date is past is dropped before CAP fetch."""
    # 2568 → 2025; cutoff 2026-05-25 is after the corrected 2025-05-25 expiry.
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    links = _parse_feed_links(_CAP_RSS_BE, now=now)
    assert links == ["https://x/be-live.xml"]


def test_parse_feed_links_keeps_live_buddhist_era_item():
    """A BE expiry whose Gregorian date is still future is kept."""
    # 2600 → 2057, comfortably after the cutoff.
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    links = _parse_feed_links(_CAP_RSS_BE, now=now)
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
async def test_fetch_gps_rejects_unparseable_coordinates_without_repeating_them():
    session = StubSession(_full_responses())
    with pytest.raises(ProviderError, match="invalid GPS") as excinfo:
        await WMOProvider().async_fetch(
            session,
            {CONF_SOURCE_ID: "mx-smn-es", CONF_GPS_LOC: "nowhere"},
            {},
            cap_content_cache=CAPContentCache(),
        )
    assert "nowhere" not in str(excinfo.value)


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
    with pytest.raises(ProviderError):
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
    with pytest.raises(ProviderError):
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
        ("  ", "en", False),
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


def test_select_info_exact_tag_beats_an_earlier_subtag_match():
    """MoWaS shape: ``de`` then ``de-LS``; asking for de-LS must reach it (#66)."""
    doc = _multilang_doc([("de", "Klar"), ("de-LS", "Einfach"), ("en", "Plain")])
    assert _select_info(doc, "de-LS").headline == "Einfach"
    assert _select_info(doc, "DE-ls").headline == "Einfach"
    assert _select_info(doc, "de").headline == "Klar"


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


def test_select_alt_info_empty_infos_is_none():
    """A ``<info>``-less document (a bare Cancel) yields no alternate.

    ``_select_info`` returns a blank block that is not in ``doc.infos``; the
    alternate lookup must take that in stride rather than raise and fail the
    whole poll for one empty document.
    """
    doc = _parse_cap_alert(
        '<?xml version="1.0"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>WMO-CANCEL</identifier><sender>t</sender>"
        "<sent>2026-08-01T12:00:00-00:00</sent><status>Actual</status>"
        "<msgType>Cancel</msgType><scope>Public</scope></alert>"
    )
    assert doc is not None
    primary = _select_info(doc, "en")
    assert _select_alt_info(doc, primary) is None


def test_select_alt_info_prefers_english_on_three_language_doc():
    """Issue #154: the alternate is English, not whichever block comes next.

    ``mo-smg-xx`` publishes ``zh-mo``/``pt-PT``/``en-US`` and ``fi-fmi-xx``
    ``fi-FI``/``sv-FI``/``en-GB``; document order handed a Chinese reader
    Portuguese and a Swedish reader Finnish.
    """
    doc = _multilang_doc(
        [("zh-mo", "暴雨"), ("pt-PT", "Chuva forte"), ("en-US", "Heavy rain")]
    )
    primary = _select_info(doc, "zh-Hans")
    assert primary is doc.infos[0]
    assert _select_alt_info(doc, primary) is doc.infos[2]

    doc = _multilang_doc(
        [("fi-FI", "Myrsky"), ("sv-FI", "Storm"), ("en-GB", "Storm warning")]
    )
    primary = _select_info(doc, "sv")
    assert primary is doc.infos[1]
    assert _select_alt_info(doc, primary) is doc.infos[2]


def test_select_alt_info_falls_back_to_document_order_without_english():
    """No English block → the first other-language block, not nothing."""
    doc = _doc("wmo_cap_multilang_no_en.xml")
    primary = _select_info(doc, "zh-Hans")
    assert primary.language == "zh-mo"
    alt = _select_alt_info(doc, primary)
    assert alt is not None
    assert alt.language == "pt-PT"


def test_select_alt_info_skips_same_language_blocks():
    """A block repeating the primary's language never becomes the alternate.

    ``ca-msc-xx`` publishes one block per area group, so "prefer English"
    would hand an English primary an English twin; ``rs-hidmet-sr`` publishes
    ``sr`` and ``sr-Latn``, one language in two scripts.
    """
    doc = _multilang_doc(
        [("en-CA", "Group 1 EN"), ("en-CA", "Group 2 EN"), ("fr-CA", "Group 1 FR")]
    )
    primary = _select_info(doc, "en-CA")
    assert primary is doc.infos[0]
    assert _select_alt_info(doc, primary) is doc.infos[2]

    doc = _multilang_doc([("en-GB", "Storm"), ("sr", "Олуја"), ("sr-Latn", "Oluja")])
    primary = _select_info(doc, "sr")
    assert primary is doc.infos[1]
    assert _select_alt_info(doc, primary) is doc.infos[0]


def test_select_alt_info_untagged_blocks_is_none():
    """Blocks without ``<language>`` cannot be shown to differ, so no alternate."""
    doc = _multilang_doc([("", "First"), ("", "Second")])
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
async def test_fetch_honours_language_option(monkeypatch):
    _pin_clock(monkeypatch, datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
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
async def test_fetch_without_language_option_is_unchanged(monkeypatch):
    _pin_clock(monkeypatch, datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
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


def test_build_alert_same_scheme_lands_in_the_container():
    alert = _alert_from_cap(_cap_with_geocodes([("SAME", "012345")]))
    assert alert.geocodes["SAME"] == ("012345",)
    assert alert.to_attributes()["geocodes"]["SAME"] == ["012345"]


def test_build_alert_without_geocodes_leaves_container_empty():
    alert = _alert_from_cap(_cap_with_geocodes([]))
    assert alert.geocodes == {}


# ---------------------------------------------------------------------------
# Feed union (issue #210)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_unions_mirror_and_bucket_dedup_by_identifier():
    session = StubSession(_union_responses())
    alerts = await _fetch(WMOProvider(), session, "tl-dnmg-en")

    assert sorted(a.identifier for a in alerts) == [_ID_1, _ID_2, _ID_3]
    assert session.requested.count(_TL_MIRROR_URL) == 1
    assert session.requested.count(_TL_BUCKET_URL) == 1


@pytest.mark.asyncio
async def test_auto_prefers_mirror_url():
    """An alert both feeds carry keeps the mirror's copy, so ``url`` is stable."""
    alerts = await _fetch(WMOProvider(), StubSession(_union_responses()), "tl-dnmg-en")
    by_id = {a.identifier: a for a in alerts}
    assert by_id[_ID_1].url == _CAP_URL_1
    assert by_id[_ID_3].url == _TL_BUCKET_CAP_3


@pytest.mark.asyncio
async def test_bucket_test_status_dropped():
    alerts = await _fetch(WMOProvider(), StubSession(_union_responses()), "tl-dnmg-en")
    assert not any(a.status == "Test" for a in alerts)
    assert not any("2.49.0.1.508" in a.identifier for a in alerts)


@pytest.mark.asyncio
async def test_bucket_expired_body_dropped_before_store():
    """A first-sight expired alert would fire incident_removed; never hand it over."""
    alerts = await _fetch(WMOProvider(), StubSession(_union_responses()), "tl-dnmg-en")
    assert "urn:wmo:mx-smn:2026:004" not in {a.identifier for a in alerts}


def test_doc_is_live_fails_open_without_a_parseable_expiry():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    live = _parse_cap_alert(_fixture("wmo_cap_1.xml"))
    assert live is not None and _wmo_mod._doc_is_live(live, now)
    no_expiry = _parse_cap_alert(
        _fixture("wmo_cap_1.xml").replace(
            "<expires>2099-05-12T23:00:00-06:00</expires>", ""
        )
    )
    assert no_expiry is not None and _wmo_mod._doc_is_live(no_expiry, now)
    garbage = _parse_cap_alert(
        _fixture("wmo_cap_1.xml").replace("2099-05-12T23:00:00-06:00", "soon")
    )
    assert garbage is not None and _wmo_mod._doc_is_live(garbage, now)
    expired = _parse_cap_alert(_fixture("wmo_cap_expired.xml"))
    assert expired is not None and not _wmo_mod._doc_is_live(expired, now)


@pytest.mark.asyncio
async def test_auto_mirror_down_serves_bucket_and_warns_once(caplog):
    responses = _union_responses()
    responses[_TL_MIRROR_URL] = (404, "")
    session = StubSession(responses)
    provider = WMOProvider()

    with caplog.at_level(logging.WARNING, logger=_wmo_mod.__name__):
        alerts = await _fetch(provider, session, "tl-dnmg-en")
        again = await _fetch(provider, session, "tl-dnmg-en")

    assert sorted(a.identifier for a in alerts) == [_ID_1, _ID_3]
    assert {a.identifier for a in again} == {a.identifier for a in alerts}
    warnings = [r for r in caplog.records if "mirror feed failed" in r.getMessage()]
    assert len(warnings) == 1
    assert "HTTP 404" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_auto_warns_again_after_a_feed_recovers(caplog):
    responses = _union_responses()
    responses[_TL_MIRROR_URL] = [(503, ""), _fixture("wmo_rss.xml"), (503, "")]
    session = StubSession(responses)
    provider = WMOProvider()
    with caplog.at_level(logging.WARNING, logger=_wmo_mod.__name__):
        for _ in range(3):
            await _fetch(provider, session, "tl-dnmg-en")
    assert sum("mirror feed failed" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.asyncio
async def test_auto_both_feeds_down_raises_naming_both():
    responses = _union_responses()
    responses[_TL_MIRROR_URL] = (503, "")
    responses[_TL_BUCKET_URL] = "not xml <<>>"
    with pytest.raises(ProviderError) as excinfo:
        await _fetch(WMOProvider(), StubSession(responses), "tl-dnmg-en")
    message = str(excinfo.value)
    assert "mirror feed" in message and "HTTP 503" in message
    assert "authority feed" in message and "parse" in message


@pytest.mark.asyncio
async def test_auto_unresolvable_source_polls_mirror_only():
    """gw-inm-en lists two bucket folders, neither its own: mirror alone."""
    responses = _union_responses()
    responses["https://severeweather.wmo.int/v2/cap-alerts/gw-inm-en/rss.xml"] = (
        _fixture("wmo_rss.xml")
    )
    session = StubSession(responses)
    provider = WMOProvider()
    alerts = await _fetch(provider, session, "gw-inm-en")
    assert sorted(a.identifier for a in alerts) == [_ID_1, _ID_2]
    assert provider.authority_feed_url is None
    assert not any("cap-sources" in url for url in session.requested)


@pytest.mark.asyncio
async def test_auto_skips_an_authority_that_is_the_mirror():
    """cn-cma-xx's record once named the mirror itself: read it once."""
    mirror = "https://severeweather.wmo.int/v2/cap-alerts/cn-cma-xx/rss.xml"
    session = StubSession(
        {
            WMO_SOURCES_URL: _fixture("wmo_sources.json"),
            mirror: _fixture("wmo_rss.xml"),
            _CAP_URL_1: _fixture("wmo_cap_1.xml"),
            _CAP_URL_2: _fixture("wmo_cap_2.xml"),
        }
    )
    provider = WMOProvider()
    alerts = await _fetch(provider, session, "cn-cma-xx")
    assert len(alerts) == 2
    assert provider.authority_feed_url == mirror
    assert session.requested.count(mirror) == 1


@pytest.mark.asyncio
async def test_mirror_mode_never_fetches_bucket():
    session = StubSession(_union_responses())
    alerts = await _fetch(
        WMOProvider(), session, "tl-dnmg-en", {CONF_FEED_SOURCE: "mirror"}
    )
    assert sorted(a.identifier for a in alerts) == [_ID_1, _ID_2]
    assert WMO_SOURCES_URL not in session.requested
    assert _TL_BUCKET_URL not in session.requested


@pytest.mark.asyncio
async def test_mirror_mode_failure_names_the_one_feed():
    responses = _union_responses()
    responses[_TL_MIRROR_URL] = (503, "")
    with pytest.raises(ProviderError, match=r"WMO tl-dnmg-en: mirror feed .*HTTP 503"):
        await _fetch(
            WMOProvider(),
            StubSession(responses),
            "tl-dnmg-en",
            {CONF_FEED_SOURCE: "mirror"},
        )


@pytest.mark.asyncio
async def test_authority_mode_never_fetches_mirror():
    session = StubSession(_union_responses())
    alerts = await _fetch(
        WMOProvider(), session, "tl-dnmg-en", {CONF_FEED_SOURCE: "authority"}
    )
    assert sorted(a.identifier for a in alerts) == [_ID_1, _ID_3]
    assert {a.identifier: a.url for a in alerts}[_ID_1] == _TL_BUCKET_CAP_1
    assert _TL_MIRROR_URL not in session.requested


@pytest.mark.asyncio
async def test_authority_mode_unresolvable_raises():
    with pytest.raises(ProviderError, match="pinned to the authority"):
        await _fetch(
            WMOProvider(),
            StubSession(_union_responses()),
            "gw-inm-en",
            {CONF_FEED_SOURCE: "authority"},
        )


@pytest.mark.asyncio
async def test_authority_mode_reads_a_national_host(monkeypatch):
    """#302's setup: th-tmd-en resolves through th-tmd-th's record to TMD's feed."""
    _pin_clock(monkeypatch, _TMD_CAPTURED_AT)
    session = StubSession(
        {
            WMO_SOURCES_URL: _fixture("wmo_sources.json"),
            _TMD_FEED_URL: _fixture("wmo_tmd_rss.xml"),
            _TMD_CAP_URL: _fixture("wmo_tmd_cap.xml"),
            # The other two listed bodies are absent → 404 → skipped.
        }
    )
    provider = WMOProvider()
    alerts = await _fetch(
        provider,
        session,
        "th-tmd-en",
        {CONF_FEED_SOURCE: "authority"},
        **{CONF_GPS_LOC: "11.8,99.75"},
    )
    assert provider.authority_feed_url == _TMD_FEED_URL
    assert _TH_MIRROR_URL not in session.requested
    assert len(alerts) == 1
    assert alerts[0].event == "Heavy Rain" and alerts[0].severity == "Severe"
    assert alerts[0].geometry is not None
    assert alerts[0].expires == "2026-10-13T06:00:00+07:00"


@pytest.mark.asyncio
async def test_registry_resolved_once_across_polls():
    session = StubSession(_union_responses())
    provider = WMOProvider()
    for _ in range(3):
        await _fetch(provider, session, "tl-dnmg-en")
    assert session.requested.count(WMO_SOURCES_URL) == 1
    assert session.requested.count(_TL_BUCKET_URL) == 3
    assert provider.authority_feed_url == _TL_BUCKET_URL


@pytest.mark.asyncio
async def test_registry_unreachable_not_cached():
    responses = _union_responses()
    responses[WMO_SOURCES_URL] = [(503, ""), _fixture("wmo_sources.json")]
    session = StubSession(responses)
    provider = WMOProvider()

    first = await _fetch(provider, session, "tl-dnmg-en")
    assert sorted(a.identifier for a in first) == [_ID_1, _ID_2]
    assert provider.authority_feed_url is None
    assert _TL_BUCKET_URL not in session.requested

    second = await _fetch(provider, session, "tl-dnmg-en")
    assert sorted(a.identifier for a in second) == [_ID_1, _ID_2, _ID_3]
    assert session.requested.count(WMO_SOURCES_URL) == 2


# ---------------------------------------------------------------------------
# Network errors on the authority path
# ---------------------------------------------------------------------------


def _connection_refused() -> Exception:
    return aiohttp.ClientConnectionError("refused")


@pytest.mark.asyncio
async def test_auto_tolerates_a_network_error_on_one_feed(caplog):
    responses = _union_responses()
    responses[_TL_BUCKET_URL] = _connection_refused
    with caplog.at_level(logging.WARNING, logger=_wmo_mod.__name__):
        alerts = await _fetch(WMOProvider(), StubSession(responses), "tl-dnmg-en")
    assert sorted(a.identifier for a in alerts) == [_ID_1, _ID_2]
    assert any(
        "authority feed failed" in r.getMessage()
        and "ClientConnectionError" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_registry_network_error_is_retried_next_poll():
    responses = _union_responses()
    responses[WMO_SOURCES_URL] = [_connection_refused, _fixture("wmo_sources.json")]
    session = StubSession(responses)
    provider = WMOProvider()
    first = await _fetch(provider, session, "tl-dnmg-en")
    second = await _fetch(provider, session, "tl-dnmg-en")
    assert sorted(a.identifier for a in first) == [_ID_1, _ID_2]
    assert sorted(a.identifier for a in second) == [_ID_1, _ID_2, _ID_3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_id", "responses", "expected"),
    [
        ("tl-dnmg-en", {}, False),  # registry unreachable
        ("zz-nobody-en", {WMO_SOURCES_URL: "wmo_sources.json"}, False),  # no record
        (
            "tl-dnmg-en",
            {WMO_SOURCES_URL: "wmo_sources.json", _TL_BUCKET_URL: "wmo_bucket_rss.xml"},
            True,
        ),
        (
            "tl-dnmg-en",
            {WMO_SOURCES_URL: "wmo_sources.json", _TL_BUCKET_URL: (503, "")},
            False,
        ),
        (
            "tl-dnmg-en",
            {WMO_SOURCES_URL: "wmo_sources.json", _TL_BUCKET_URL: "<<not xml"},
            False,
        ),
        (
            "tl-dnmg-en",
            {WMO_SOURCES_URL: "wmo_sources.json", _TL_BUCKET_URL: _connection_refused},
            False,
        ),
    ],
)
async def test_async_authority_feed_available(
    source_id: str, responses: dict[str, Any], expected: bool
):
    """The options-flow check behind pinning ``authority`` (#210)."""
    stub = {
        url: _fixture(body)
        if isinstance(body, str) and body.endswith((".json", ".xml"))
        else body
        for url, body in responses.items()
    }
    assert (
        await _wmo_mod.async_authority_feed_available(
            StubSession(stub), source_id, user_agent="test"
        )
        is expected
    )
