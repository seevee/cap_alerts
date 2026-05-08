"""Shared LRU cache for immutable CAP XML document bodies."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

import aiohttp

_LOGGER = logging.getLogger(__name__)


class CAPContentCache:
    """LRU cache for CAP XML bodies with Future-based in-flight coalescing.

    CAP files are immutable per URL (each revision gets a new URL), so a
    successful fetch is cached indefinitely up to ``max_entries``.  Two
    concurrent callers for the same URL share one HTTP GET via the
    ``_inflight`` dict.
    """

    def __init__(self, max_entries: int = 256) -> None:
        self._max_entries = max_entries
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[str | None]] = {}

    async def get_or_fetch(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        user_agent: str | None = None,
    ) -> str | None:
        """Return cached body for URL, fetching on miss.

        Returns None on HTTP error, network error, or timeout.  Logs a
        warning at most once per URL per cache instance.
        """
        if url in self._cache:
            self._cache.move_to_end(url)
            return self._cache[url]

        if url in self._inflight:
            return await self._inflight[url]

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        self._inflight[url] = fut

        result: str | None = None
        try:
            headers: dict[str, str] = {}
            if user_agent:
                headers["User-Agent"] = user_agent
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    _LOGGER.warning("CAP fetch HTTP %s for %s", resp.status, url)
                else:
                    result = await resp.text()
            if result is not None:
                self._cache[url] = result
                self._cache.move_to_end(url)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.warning("CAP fetch failed for %s: %s", url, exc)
        finally:
            if not fut.done():
                fut.set_result(result)
            self._inflight.pop(url, None)

        return result
