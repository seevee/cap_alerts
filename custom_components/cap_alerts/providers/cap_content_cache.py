"""Shared LRU cache for immutable fetched bodies: CAP XML, CAP JSON, GeoJSON."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import OrderedDict
from collections.abc import Mapping

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Memory budget for the process-wide body cache, not an entry count.
#
# An entry count cannot bound memory here, because CAP body size varies by two
# orders of magnitude across sources: sampled 2026-08-04, cn-cma-xx bodies
# average 88.9 KiB and peak at 520 KiB, while a typical NWS or MeteoAlarm body
# is a few KiB. The previous ``max_entries=256`` was therefore simultaneously
# too large (≈22 MiB of CMA XML) and far too small to be useful: cn-cma-xx
# needs 501 distinct URLs per poll, so a single poll evicted its own earliest
# entries before finishing and every subsequent poll re-fetched all 501 from
# cold — ~32 s, past the 30 s default timeout, so the entry never completed an
# update. Being shared domain-wide, it also evicted every other entry's bodies.
#
# 64 MiB holds cn-cma-xx's full working set (~44 MiB) with room for the other
# entries, and bounds worst-case growth regardless of how large a body gets.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

# Per-request ceiling for one body fetch. Sized for a CAP XML document of a
# few KiB to a few hundred KiB; a caller fetching something larger passes its
# own (GDACS geometry, issue #196). The coordinator's own poll budget wraps
# every fetch either way, so this is the bound on one slow document, not on
# the poll.
DEFAULT_FETCH_TIMEOUT = 10  # seconds

# A failed fetch is never stored, so the next poll retries it and, without
# this, warns again: a body that fails every poll logs every poll. PAGASA's
# own feed links ten bodies on a raw IP whose certificate never matches, 20
# lines per poll across the cache and the provider before #303. The first
# failure per URL warns, later ones log at debug, and a success clears the
# mark so a URL that recovers and fails again warns again. Bounded so a
# feed that rotates failing URLs cannot grow the set without limit.
_MAX_WARNED_URLS = 256


def _validators_from(response_headers: Mapping[str, str]) -> dict[str, str]:
    """The conditional-request headers a ``200``'s validators allow."""
    headers: dict[str, str] = {}
    if etag := response_headers.get("ETag"):
        headers["If-None-Match"] = etag
    if modified := response_headers.get("Last-Modified"):
        headers["If-Modified-Since"] = modified
    return headers


class Redirected(Exception):
    """A fetch made with ``follow_redirects=False`` was answered with a 3xx.

    ``url`` is what was asked for, ``location`` where the host pointed
    instead (empty when it named nowhere). Raised to every caller sharing
    the in-flight fetch; nothing is cached.
    """

    def __init__(self, url: str, location: str) -> None:
        super().__init__(f"{url} redirected to {location or '(no Location)'}")
        self.url = url
        self.location = location


class CAPContentCache:
    """LRU cache for CAP XML bodies with Future-based in-flight coalescing.

    CAP files are immutable per URL (each revision gets a new URL), so a
    successful fetch is cached indefinitely until evicted under memory
    pressure. Two concurrent callers for the same URL share one HTTP GET via
    the ``_inflight`` dict.

    A body never changes under its URL, but whether the host still serves it
    can: BBK withdraws a warning by redirecting its document URL into an
    archive (#295). A caller that has to see that asks for ``revalidate``,
    and a hit then costs one conditional GET with the validators the body
    arrived under, which a live document answers with ``304`` and no body.

    Eviction is LRU against a **byte budget** (``max_bytes``), measured with
    ``sys.getsizeof`` so the bound tracks real memory rather than a character
    count — CAP bodies are largely non-ASCII for several sources, where a
    Python ``str`` costs well over one byte per character.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._sizes: dict[str, int] = {}
        # Conditional-request headers per cached URL, from the 200 that
        # stored the body; evicted with it. Empty when the host sent none.
        self._validators: dict[str, dict[str, str]] = {}
        self._total_bytes = 0
        self._inflight: dict[str, asyncio.Future[str | None]] = {}
        # URLs whose last fetch failed and has been warned about, oldest first.
        self._warned: OrderedDict[str, None] = OrderedDict()

    def _warn_once(self, url: str, msg: str, *args: object) -> None:
        """Warn the first time ``url`` fails, debug on every later failure."""
        if url in self._warned:
            _LOGGER.debug(msg, *args)
            return
        self._warned[url] = None
        while len(self._warned) > _MAX_WARNED_URLS:
            self._warned.popitem(last=False)
        _LOGGER.warning(msg, *args)

    @property
    def total_bytes(self) -> int:
        """Current cached payload size, for diagnostics and tests."""
        return self._total_bytes

    def __len__(self) -> int:
        return len(self._cache)

    def _evict(self, url: str) -> None:
        """Drop ``url``'s body and validators, if held."""
        if self._cache.pop(url, None) is not None:
            self._total_bytes -= self._sizes.pop(url, 0)
        self._validators.pop(url, None)

    def _store(
        self, url: str, body: str, validators: dict[str, str] | None = None
    ) -> None:
        """Insert ``body`` and evict least-recently-used entries over budget.

        A body larger than the whole budget is not cached at all — storing it
        would evict everything else and then be evicted itself on the next
        insert, which is strictly worse than a miss. Whatever was held under
        the URL is dropped either way: it is no longer what the host serves.
        """
        self._evict(url)
        size = sys.getsizeof(body)
        if size > self._max_bytes:
            _LOGGER.debug(
                "CAP body for %s is %d bytes, over the %d byte cache budget; not caching",
                url,
                size,
                self._max_bytes,
            )
            return
        self._cache[url] = body
        self._sizes[url] = size
        if validators:
            self._validators[url] = validators
        self._total_bytes += size
        while self._total_bytes > self._max_bytes and self._cache:
            oldest, _ = self._cache.popitem(last=False)
            self._total_bytes -= self._sizes.pop(oldest, 0)
            self._validators.pop(oldest, None)

    async def get_or_fetch(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        user_agent: str | None = None,
        timeout: float = DEFAULT_FETCH_TIMEOUT,
        follow_redirects: bool = True,
        revalidate: bool = False,
    ) -> str | None:
        """Return cached body for URL, fetching on miss.

        ``timeout`` bounds one HTTP request in seconds; a cache hit or an
        in-flight coalesce never waits on it.

        ``follow_redirects=False`` makes a 3xx answer raise ``Redirected``
        instead of being followed. A feed whose document URLs are keyed by an
        immutable revision id never redirects a live document, so a redirect
        there is the host answering with something else: a withdrawn warning's
        archive copy, say, in a shape that parses as a live alert (#295). The
        caller decides what the Location means; the body behind it is never
        fetched.

        ``revalidate=True`` asks the host again on a hit, conditionally, for a
        caller that must see a URL stop being served. ``304`` serves the cached
        body; a redirect (under ``follow_redirects=False``), ``404`` or ``410``
        is the host's answer and evicts it. A network error, timeout or other
        status is not an answer about the document, so the cached body is
        served: for a source whose alerts end on absence, a dropped request
        would otherwise end a live warning.

        Returns None on HTTP error, network error, or timeout. Logs a
        warning the first time a URL fails and at debug on later failures,
        until a fetch of it succeeds (``_warn_once``).
        """
        cached = self._cache.get(url)
        if cached is not None and not revalidate:
            self._cache.move_to_end(url)
            return cached

        if url in self._inflight:
            # Shielded: a waiter cancelled here (an unload, or its own timeout)
            # must not cancel the fetch every other caller is sharing.
            return await asyncio.shield(self._inflight[url])

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        self._inflight[url] = fut

        result: str | None = None
        try:
            headers: dict[str, str] = {}
            if user_agent:
                headers["User-Agent"] = user_agent
            if cached is not None:
                headers.update(self._validators.get(url, {}))
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with session.get(
                url,
                headers=headers,
                timeout=client_timeout,
                allow_redirects=follow_redirects,
            ) as resp:
                if resp.status == 304 and cached is not None:
                    result = cached
                    self._cache.move_to_end(url)
                elif not follow_redirects and 300 <= resp.status < 400:
                    raise Redirected(url, resp.headers.get("Location", ""))
                elif resp.status == 200:
                    result = await resp.text()
                    self._warned.pop(url, None)
                    self._store(url, result, _validators_from(resp.headers))
                elif cached is not None and resp.status not in (404, 410):
                    _LOGGER.debug(
                        "CAP revalidation HTTP %s for %s; serving the cached body",
                        resp.status,
                        url,
                    )
                    result = cached
                else:
                    self._evict(url)
                    self._warn_once(url, "CAP fetch HTTP %s for %s", resp.status, url)
        except Redirected as exc:
            self._evict(url)
            fut.set_exception(exc)
            # Coalesced waiters re-raise it on await; mark it retrieved so a
            # fetch nobody else was waiting on does not log "exception was
            # never retrieved" when the future is collected.
            fut.exception()
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError: resp.text() decodes strictly, with the header
            # charset or UTF-8, so a body in another encoding fails here. It is
            # one document, so it costs one alert rather than the poll (#371).
            if cached is not None:
                _LOGGER.debug(
                    "CAP revalidation failed for %s: %s; serving the cached body",
                    url,
                    exc,
                )
                result = cached
            else:
                self._warn_once(url, "CAP fetch failed for %s: %s", url, exc)
        finally:
            if not fut.done():
                fut.set_result(result)
            self._inflight.pop(url, None)

        return result
