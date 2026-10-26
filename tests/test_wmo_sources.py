"""Tests for the WMO SWIC registry: the config-flow dropdown and the
authority feed resolver (issue #210)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.cap_alerts.const import WMO_SOURCES_URL
from custom_components.cap_alerts.providers import wmo as _wmo_mod
from tests.conftest import StubSession

_FIXTURES = Path(__file__).parent / "fixtures"
_BUCKET = "https://cap-sources.s3.amazonaws.com/"


fetch_wmo_sources = _wmo_mod.fetch_wmo_sources
authority_feed_url = _wmo_mod.authority_feed_url
_wmo_source_label = _wmo_mod._wmo_source_label


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _registry() -> Any:
    return json.loads(_fixture("wmo_sources.json"))


def _responses(body: Any) -> dict[str, Any]:
    return {WMO_SOURCES_URL: body}


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_includes_overlapping_providers():
    """Sources also covered by MeteoAlarm / NWS are not filtered out."""
    session = StubSession(_responses(_fixture("wmo_sources.json")))
    sources = await fetch_wmo_sources(session)
    ids = {sid for sid, _label in sources}
    assert "at-zamg-en" in ids  # MeteoAlarm member
    assert "us-noaa-nws-en" in ids  # NWS feed


@pytest.mark.asyncio
async def test_unmirrored_source_is_listed():
    """Nothing is filtered since #210: every record resolves to some feed.

    ``co-ungrd-es`` was never on the mirror and its record names a national
    host; the source step's validation decides whether that feed answers,
    not a point-in-time list.
    """
    session = StubSession(_responses(_fixture("wmo_sources.json")))
    sources = await fetch_wmo_sources(session)
    ids = {sid for sid, _label in sources}
    assert "co-ungrd-es" in ids
    assert "mz-inam-en" in ids


@pytest.mark.asyncio
async def test_includes_sources_sorted():
    """Expected source IDs are present, sorted by label (case-insensitive)."""
    session = StubSession(_responses(_fixture("wmo_sources.json")))
    sources = await fetch_wmo_sources(session)
    ids = [sid for sid, _label in sources]
    assert ids == [
        "at-zamg-en",
        "cn-cma-xx",
        "co-ungrd-es",
        "gw-inm-en",
        "id-inatews-id",
        "mx-smn-es",
        "mz-inam-en",
        "ph-pagasa-en",
        "sb-sims-en",
        "th-tmd-th",
        "tl-dnmg-en",
        "us-noaa-nws-en",
        "us-noaa-nws-en-marine",
    ]
    labels = [label for _sid, label in sources]
    assert labels == sorted(labels, key=str.lower)


# ---------------------------------------------------------------------------
# Label format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_format():
    """Labels are '{countryName} ({AUTHORITYABBREV}, {langs})' from byLanguage.

    The source-ID trailing segment is not the body language: ``cn-cma-xx``
    ends ``-xx`` yet publishes ``zh-CN``.
    """
    session = StubSession(_responses(_fixture("wmo_sources.json")))
    sources = dict(await fetch_wmo_sources(session))
    assert sources["cn-cma-xx"] == "China (CMA, zh)"
    assert sources["mx-smn-es"] == "Mexico (SMN, es)"


def test_label_lists_every_language_of_a_multilingual_source():
    """Multi-valued labels flag the sources worth setting a language on."""
    assert (
        _wmo_source_label(
            {
                "sourceId": "at-zamg-en",
                "countryName": "Austria",
                "authorityAbbrev": "ZAMG",
                "byLanguage": [{"code": "de-DE"}, {"code": "en-GB"}],
            }
        )
        == "Austria (ZAMG, de/en)"
    )


def test_label_deduplicates_language_subtags():
    assert (
        _wmo_source_label(
            {
                "sourceId": "ca-aema-xx",
                "countryName": "Canada",
                "authorityAbbrev": "AEMA",
                "byLanguage": [{"code": "en-CA"}, {"code": "fr-CA"}, {"code": "en-US"}],
            }
        )
        == "Canada (AEMA, en/fr)"
    )


def test_label_without_bylanguage_omits_the_segment():
    """No languages → no trailing comma."""
    assert (
        _wmo_source_label(
            {
                "sourceId": "xx-foo-en",
                "countryName": "Country",
                "authorityAbbrev": "ABC",
            }
        )
        == "Country (ABC)"
    )
    assert (
        _wmo_source_label(
            {
                "sourceId": "xx-foo-en",
                "countryName": "Country",
                "authorityAbbrev": "ABC",
                "byLanguage": [],
            }
        )
        == "Country (ABC)"
    )


def test_label_falls_back_to_bylanguage_name():
    """Missing country/abbrev → first byLanguage name; then bare source ID."""
    assert (
        _wmo_source_label(
            {"sourceId": "xx-foo-en", "byLanguage": [{"name": "Fallback Name"}]}
        )
        == "Fallback Name"
    )
    assert _wmo_source_label({"sourceId": "xx-foo-en"}) == "xx-foo-en"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_failure_returns_empty_non_200():
    session = StubSession(_responses((503, "")))
    assert await fetch_wmo_sources(session) == []


@pytest.mark.asyncio
async def test_fetch_failure_returns_empty_bad_json():
    session = StubSession(_responses("not json <<>>"))
    assert await fetch_wmo_sources(session) == []


@pytest.mark.asyncio
async def test_fetch_unexpected_shape_returns_empty():
    session = StubSession(_responses('{"unexpected": true}'))
    assert await fetch_wmo_sources(session) == []


# ---------------------------------------------------------------------------
# Authority feed resolution (issue #210)
# ---------------------------------------------------------------------------


def test_authority_feed_url_matches_folder():
    """A bucket folder named like the id wins, whichever record lists it."""
    assert (
        authority_feed_url(_registry(), "tl-dnmg-en") == f"{_BUCKET}tl-dnmg-en/rss.xml"
    )
    assert (
        authority_feed_url(_registry(), "tl-dnmg-pt") == f"{_BUCKET}tl-dnmg-pt/rss.xml"
    )
    assert (
        authority_feed_url(_registry(), "TL-DNMG-TET ")
        == f"{_BUCKET}tl-dnmg-tet/rss.xml"
    )
    assert (
        authority_feed_url(_registry(), "mz-inam-pt") == f"{_BUCKET}mz-inam-pt/rss.xml"
    )


def test_authority_feed_url_falls_back_to_single_url_record():
    """sb-sims-en's one folder is sb-met-en: the record's only URL is the feed."""
    assert (
        authority_feed_url(_registry(), "sb-sims-en") == f"{_BUCKET}sb-met-en/rss.xml"
    )


def test_authority_feed_url_two_url_record_is_unresolvable():
    """gw-inm-en lists gw-inm-fr and gw-inm-pt, neither its own id."""
    assert authority_feed_url(_registry(), "gw-inm-en") is None


def test_authority_feed_url_national_host_single_url():
    assert (
        authority_feed_url(_registry(), "mx-smn-es")
        == "https://smn.conagua.gob.mx/tools/GUI/webservices/cap.xml"
    )


def test_authority_feed_url_picks_url_by_language_index():
    """th-tmd-th lists [en, th] against two URLs; a mirror id picks by its tag."""
    assert (
        authority_feed_url(_registry(), "th-tmd-en")
        == "https://www.tmd.go.th/en/api/xml/CAP"
    )
    assert (
        authority_feed_url(_registry(), "th-tmd-th")
        == "https://www.tmd.go.th/api/xml/CAP"
    )
    # An unlisted tag falls back to the record's first URL.
    assert (
        authority_feed_url(_registry(), "th-tmd-xx")
        == "https://www.tmd.go.th/en/api/xml/CAP"
    )


def test_authority_feed_url_prefix_match_when_id_is_not_a_record():
    """``th-tmd-en`` has no record; ``th-tmd-`` matches exactly one."""
    registry = _registry()
    assert not any(e["source"]["sourceId"] == "th-tmd-en" for e in registry["sources"])
    assert authority_feed_url(registry, "th-tmd-en") is not None
    # ``us-noaa-`` matches two records: ambiguous, so nothing.
    assert authority_feed_url(registry, "us-noaa-zz") is None


def test_authority_feed_url_no_record_is_none():
    assert authority_feed_url(_registry(), "zz-nobody-en") is None
    assert authority_feed_url(_registry(), "") is None
    assert authority_feed_url({"unexpected": True}, "mx-smn-es") is None
    assert authority_feed_url("not a mapping", "mx-smn-es") is None


def test_authority_feed_url_record_without_feed_is_none():
    registry = _registry()
    for entry in registry["sources"]:
        if entry["source"]["sourceId"] == "mx-smn-es":
            entry["source"]["capAlertFeed"] = ""
    assert authority_feed_url(registry, "mx-smn-es") is None


def test_authority_feed_url_language_index_beyond_urls_falls_back():
    """More byLanguage entries than URLs: the matching index may not exist."""
    registry = _registry()
    for entry in registry["sources"]:
        if entry["source"]["sourceId"] == "th-tmd-th":
            entry["source"]["byLanguage"].insert(0, {"code": "en-GB"})
            entry["source"]["byLanguage"].append({"code": "lo"})
    assert (
        authority_feed_url(registry, "th-tmd-lo")
        == "https://www.tmd.go.th/en/api/xml/CAP"
    )
