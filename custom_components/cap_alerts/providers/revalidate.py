"""Conditional GET for feeds that are re-read whole every poll (issue #304).

An index feed is one document the provider downloads each cycle to learn
which CAP bodies exist. Most sit still between polls, and a host that sends
``ETag`` or ``Last-Modified`` will answer ``304 Not Modified`` with no body
to a request that quotes it back. ``FeedValidators`` remembers, per URL, the
validators of the last ``200`` and the body they vouch for; ``fetch_feed_text``
sends them and replays that body on a ``304``, so a quiet feed costs one
request and no transfer.

ECCC revalidates its GeoRSS hosts the same way inside its own retry loop;
this is the provider-neutral form, for WMO's index feeds and the TWC product
slots today and any single-document feed (the Australian state feeds) later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import aiohttp

from . import ProviderError


class FeedStatusError(ProviderError):
    """A feed answered with a status other than ``200``; ``status`` says which.

    For a caller that treats one status differently from the rest (WMO backs
    a retired mirror off on ``404``, issue #333). Everything else sees the
    plain ``ProviderError`` it always did, message included.
    """

    def __init__(self, status: int, message: str | None = None) -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class _LastRead:
    """What the last ``200`` for a URL left behind."""

    request_headers: dict[str, str]
    body: str


class FeedValidators:
    """The validators and bodies of the last successful read, per feed URL."""

    def __init__(self) -> None:
        self._last: dict[str, _LastRead] = {}

    def __len__(self) -> int:
        return len(self._last)

    def request_headers(self, url: str) -> dict[str, str]:
        """The conditional headers to send for ``url``; empty on a first read."""
        last = self._last.get(url)
        return dict(last.request_headers) if last else {}

    def remember(
        self, url: str, response_headers: Mapping[str, str], body: str
    ) -> None:
        """Keep ``body`` under the validators ``response_headers`` carry.

        ``ETag`` becomes ``If-None-Match`` and ``Last-Modified`` becomes
        ``If-Modified-Since``; a host that sends neither has nothing to
        revalidate against, and any earlier memory for the URL is dropped
        rather than replayed against a response that no longer vouches for it.
        """
        headers: dict[str, str] = {}
        if etag := response_headers.get("ETag"):
            headers["If-None-Match"] = etag
        if modified := response_headers.get("Last-Modified"):
            headers["If-Modified-Since"] = modified
        if headers:
            self._last[url] = _LastRead(headers, body)
        else:
            self._last.pop(url, None)

    def replay(self, url: str) -> str | None:
        """The body the last ``200`` for ``url`` returned, or ``None``."""
        last = self._last.get(url)
        return last.body if last else None


async def fetch_feed_text(
    session: aiohttp.ClientSession,
    url: str,
    validators: FeedValidators | None = None,
    *,
    user_agent: str | None = None,
) -> str:
    """GET ``url`` and return its body, replaying the last one on a ``304``.

    ``FeedStatusError`` on any status but ``200`` (or a ``304`` with nothing
    remembered to replay, which a host should never send), ``ProviderError``
    on a client error. Without ``validators`` this is a plain GET.
    """
    headers: dict[str, str] = {}
    if user_agent:
        headers["User-Agent"] = user_agent
    if validators is not None:
        headers.update(validators.request_headers(url))
    try:
        async with session.get(url, headers=headers or None) as resp:
            if resp.status == 304 and validators is not None:
                body = validators.replay(url)
                if body is None:
                    raise FeedStatusError(
                        304, "HTTP 304 with no earlier body to replay"
                    )
                return body
            if resp.status != 200:
                raise FeedStatusError(resp.status)
            text = await resp.text()
            response_headers = resp.headers
    except aiohttp.ClientError as err:
        raise ProviderError(f"{type(err).__name__}: {err}") from err
    if validators is not None:
        validators.remember(url, response_headers, text)
    return text
