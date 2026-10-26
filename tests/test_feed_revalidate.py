"""Conditional GET for index feeds (issue #304): ``providers/revalidate.py``.

A host that sends ``ETag`` or ``Last-Modified`` answers ``304`` to a request
that quotes it back, and the helper replays the body of the last ``200``. A
host that sends neither is read plain every time.
"""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.cap_alerts.providers import ProviderError
from custom_components.cap_alerts.providers.revalidate import (
    FeedStatusError,
    FeedValidators,
    fetch_feed_text,
)
from tests.conftest import StubSession

URL = "https://feed.example/rss.xml"


def test_validators_map_response_headers_to_request_headers():
    validators = FeedValidators()
    assert validators.request_headers(URL) == {}

    validators.remember(
        URL, {"ETag": '"abc"', "Last-Modified": "Tue, 20 Oct 2026 20:07:11 GMT"}, "b"
    )
    assert validators.request_headers(URL) == {
        "If-None-Match": '"abc"',
        "If-Modified-Since": "Tue, 20 Oct 2026 20:07:11 GMT",
    }
    assert validators.replay(URL) == "b"


def test_a_response_without_validators_forgets_the_url():
    validators = FeedValidators()
    validators.remember(URL, {"ETag": '"abc"'}, "old")
    validators.remember(URL, {}, "new")
    assert validators.request_headers(URL) == {}
    assert validators.replay(URL) is None
    assert len(validators) == 0


@pytest.mark.asyncio
async def test_second_read_sends_the_validators_and_replays_on_304():
    session = StubSession({URL: [(200, "<rss/>", {"ETag": '"v1"'}), (304, "")]})
    validators = FeedValidators()

    first = await fetch_feed_text(session, URL, validators, user_agent="ua/1")
    second = await fetch_feed_text(session, URL, validators, user_agent="ua/1")

    assert first == second == "<rss/>"
    assert session.request_headers[0] == {"User-Agent": "ua/1"}
    assert session.request_headers[1] == {"User-Agent": "ua/1", "If-None-Match": '"v1"'}


@pytest.mark.asyncio
async def test_a_changed_feed_replaces_the_memory():
    session = StubSession(
        {URL: [(200, "one", {"ETag": '"v1"'}), (200, "two", {"ETag": '"v2"'})]}
    )
    validators = FeedValidators()
    await fetch_feed_text(session, URL, validators)
    assert await fetch_feed_text(session, URL, validators) == "two"
    assert validators.request_headers(URL) == {"If-None-Match": '"v2"'}


@pytest.mark.asyncio
async def test_last_modified_only_hosts_revalidate_too():
    """The SWIC mirror sends ``Last-Modified`` and no ``ETag``."""
    stamp = "Tue, 20 Oct 2026 20:11:24 GMT"
    session = StubSession({URL: [(200, "<rss/>", {"Last-Modified": stamp}), (304, "")]})
    validators = FeedValidators()
    await fetch_feed_text(session, URL, validators)
    assert await fetch_feed_text(session, URL, validators) == "<rss/>"
    assert session.request_headers[1] == {"If-Modified-Since": stamp}


@pytest.mark.asyncio
async def test_304_with_nothing_to_replay_is_an_error():
    """A host should never 304 a request that carried no validators."""
    with pytest.raises(ProviderError, match="304"):
        await fetch_feed_text(StubSession({URL: (304, "")}), URL, FeedValidators())


@pytest.mark.asyncio
async def test_304_without_validators_is_a_plain_status_error():
    with pytest.raises(ProviderError, match="HTTP 304"):
        await fetch_feed_text(StubSession({URL: (304, "")}), URL)


@pytest.mark.asyncio
async def test_without_validators_it_is_a_plain_get():
    session = StubSession({URL: (200, "<rss/>", {"ETag": '"v1"'})})
    assert await fetch_feed_text(session, URL) == "<rss/>"
    await fetch_feed_text(session, URL)
    assert session.request_headers == [None, None]


@pytest.mark.asyncio
async def test_non_200_and_client_errors_are_provider_errors():
    with pytest.raises(ProviderError, match="HTTP 503"):
        await fetch_feed_text(StubSession({URL: (503, "")}), URL)
    with pytest.raises(ProviderError, match="ClientConnectionError"):
        await fetch_feed_text(
            StubSession({URL: lambda: aiohttp.ClientConnectionError("down")}), URL
        )


@pytest.mark.asyncio
async def test_a_status_error_carries_the_status():
    """WMO backs a retired mirror off on 404 (issue #333) and needs to tell."""
    with pytest.raises(FeedStatusError, match="^HTTP 404$") as excinfo:
        await fetch_feed_text(StubSession({URL: (404, "")}), URL)
    assert excinfo.value.status == 404
    with pytest.raises(FeedStatusError) as replay:
        await fetch_feed_text(StubSession({URL: (304, "")}), URL, FeedValidators())
    assert replay.value.status == 304
