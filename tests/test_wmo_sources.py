"""Tests for the WMO SWIC dynamic source list (config-flow dropdown)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from custom_components.cap_alerts.const import WMO_SOURCES_URL
from custom_components.cap_alerts.providers import wmo as _wmo_mod
from tests.conftest import StubSession

_FIXTURES = Path(__file__).parent / "fixtures"


fetch_wmo_sources = _wmo_mod.fetch_wmo_sources
_wmo_source_label = _wmo_mod._wmo_source_label


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


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
async def test_excludes_unmirrored():
    """The only filter drops known-unmirrored (mirror-404) sources."""
    session = StubSession(_responses(_fixture("wmo_sources.json")))
    sources = await fetch_wmo_sources(session)
    ids = {sid for sid, _label in sources}
    assert "co-ungrd-es" not in ids


@pytest.mark.asyncio
async def test_includes_sources_sorted():
    """Expected source IDs are present, sorted by label (case-insensitive)."""
    session = StubSession(_responses(_fixture("wmo_sources.json")))
    sources = await fetch_wmo_sources(session)
    ids = [sid for sid, _label in sources]
    assert ids == [
        "at-zamg-en",
        "cn-cma-xx",
        "id-inatews-id",
        "mx-smn-es",
        "ph-pagasa-en",
        "us-noaa-nws-en",
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
