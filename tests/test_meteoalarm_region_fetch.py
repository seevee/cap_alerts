"""``fetch_regions_for_country``: language selection and failure signalling.

The region picker is derived from the warnings feed — no usable regions
endpoint exists (see the provider docstring). A multi-language feed repeats
its areas per language, and for the countries whose areas carry no
region-selectable geocode the picker falls back to ``areaDesc``, where the
label *is* the code: reading every ``<info>`` block there offers each region
once per published language. Norway published 26 entries for 13 regions that
way; the fixture is three of those warnings, trimmed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.cap_alerts.providers import meteoalarm

from homeassistant.helpers.update_coordinator import UpdateFailed

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


class _FakeResponse:
    def __init__(self, payload: str, status: int = 200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):
        return json.loads(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    """Serves one canned body and records every URL requested."""

    def __init__(self, body: dict | str, status: int = 200):
        self._body = body if isinstance(body, str) else json.dumps(body)
        self._status = status
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        return _FakeResponse(self._body, self._status)


def _no_payload() -> dict:
    return json.loads((_FIXTURE_DIR / "meteoalarm_no.json").read_text(encoding="utf-8"))


# ── language selection ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_areadesc_regions_are_not_duplicated_per_language():
    session = _RecordingSession(_no_payload())
    regions = await meteoalarm.fetch_regions_for_country(session, "NO")
    labels = [label for _code, label in regions]
    # Three warnings, each published in Norwegian and English: one entry each,
    # not two.
    assert len(regions) == 3
    assert "Oestlandet and parts of Agder" in labels
    assert "Østlandet og deler av Agder" not in labels
    # areaDesc namespace: the code and the label are the same string.
    assert all(code == label for code, label in regions)


@pytest.mark.asyncio
async def test_language_selects_the_matching_info_block():
    # met.no tags its Norwegian block ``no``, not ``nb`` — the tag has to be
    # the feed's, since ``_pick_info_blocks`` matches on the 2-letter prefix
    # and treats a near-miss as no match at all.
    session = _RecordingSession(_no_payload())
    regions = await meteoalarm.fetch_regions_for_country(session, "NO", language="no")
    labels = [label for _code, label in regions]
    assert len(regions) == 3
    assert "Østlandet og deler av Agder" in labels
    assert "Oestlandet and parts of Agder" not in labels


@pytest.mark.asyncio
async def test_unknown_language_falls_back_to_english():
    # ``_pick_info_blocks`` prefers ``en`` over document order, so a language
    # the feed does not publish lands on the English block rather than on
    # whichever block happens to come first.
    session = _RecordingSession(_no_payload())
    regions = await meteoalarm.fetch_regions_for_country(session, "NO", language="zh")
    labels = [label for _code, label in regions]
    assert "Oestlandet and parts of Agder" in labels


# ── request shape ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_the_warnings_feed_is_requested():
    # The regions endpoint is 404 for all 38 countries and is no longer
    # probed; a reinstated probe would cost a round-trip per form render.
    session = _RecordingSession(_no_payload())
    await meteoalarm.fetch_regions_for_country(session, "NO")
    assert session.requested == [
        "https://feeds.meteoalarm.org/api/v1/warnings/feeds-norway"
    ]


# ── empty vs. failed ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_feed_returns_no_regions():
    # Iceland and Malta look like this: the feed reads fine and names nothing.
    # Distinct from a failure, so the config flow can say so.
    session = _RecordingSession({"warnings": []})
    assert await meteoalarm.fetch_regions_for_country(session, "IS") == []


@pytest.mark.asyncio
async def test_http_error_raises():
    session = _RecordingSession({}, status=500)
    with pytest.raises(UpdateFailed):
        await meteoalarm.fetch_regions_for_country(session, "NO")


@pytest.mark.asyncio
async def test_invalid_json_raises():
    session = _RecordingSession("not json")
    with pytest.raises(UpdateFailed):
        await meteoalarm.fetch_regions_for_country(session, "NO")


@pytest.mark.asyncio
async def test_missing_warnings_array_raises():
    session = _RecordingSession({"something": "else"})
    with pytest.raises(UpdateFailed):
        await meteoalarm.fetch_regions_for_country(session, "NO")


@pytest.mark.asyncio
async def test_unsupported_country_raises():
    session = _RecordingSession(_no_payload())
    with pytest.raises(UpdateFailed):
        await meteoalarm.fetch_regions_for_country(session, "ZZ")
    assert session.requested == []
