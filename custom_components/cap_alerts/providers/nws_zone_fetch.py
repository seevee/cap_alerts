"""Putting the NWS zone dataset on disk (issue #316).

``providers/nws_zone_data.py`` reads a verified file from one fixed path and
does not care how it got there. This module is one way it gets there: fetch it
from an address, check it, and put it in place. Whatever starts the fetch (an
options step, an update entity, a repair) calls the same function, so there is
one download path to reason about and one place it can go wrong.

Nothing here decides *when* to fetch. No timer, no check for something newer,
no call at setup: it runs when a person asks for it and not otherwise.

It is written on the assumption that whatever answers the request is hostile:
a compromised host, a poisoned cache, a machine in the middle. The caller
names the exact dataset it wants by SHA-256, and nothing else is accepted. The
steps are in a fixed order, and each protects the next:

1. **Only where it was sent.** The address must be ``https`` with no
   credentials in it. Redirects are followed by hand, a few at most, and each
   hop must again be ``https`` and on a host the caller allowed. A response
   cannot bounce the request to plain ``http`` or to a device on the local
   network.
2. **Raw bytes only.** The request asks for no transfer compression and a
   response that applies one anyway is refused unread, so the HTTP client is
   never asked to decompress something an attacker sent.
3. **Fetch, with a ceiling and a clock.** The body is read in chunks and
   abandoned the moment it passes ``MAX_ZONE_DATA_BYTES`` or the timeout,
   whatever its headers claimed. A size the server announced must be the size
   that arrives.
4. **Verify, before anything is written.** The SHA-256 must be the one asked
   for. That alone decides whether the bytes are looked at any further: a
   file that is not the expected dataset is never decompressed and never
   parsed. An older published dataset is refused too, so the download cannot
   be used to roll an install back to stale zones.
5. **Write to the side, then rename.** The bytes land in a ``.part`` file
   beside the target, made new each time, private, and never through a
   symbolic link or into anything already there, and are renamed over it. The fixed path holds either the old file or the new
   one, never half of either.
6. **Swap the copy in memory.** Entries already running get the new dataset.

Every failure raises ``ZoneDataFetchError`` with a ``reason`` a screen can show,
and leaves the fixed path exactly as it was.

``async_remove_zone_data`` is the way back out: it deletes the file, under the
same lock as an install.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Final

import aiohttp
from homeassistant.core import HomeAssistant
from yarl import URL

from ..const import DOMAIN
from .nws_zone_data import (
    KNOWN_ZONE_DATA,
    MAX_ZONE_DATA_BYTES,
    REASON_TOO_LARGE,
    REASON_UNRECOGNISED,
    ZoneDataUnavailableError,
    async_reload_zone_data,
    read_verified,
    verify_zone_data,
    zone_data_path,
)

_LOGGER = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT: Final = 120  # seconds, for the whole transfer
MAX_REDIRECTS: Final = 5
_CHUNK: Final = 64 * 1024
_LOCK_KEY: Final = "nws_zone_fetch_lock"
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})

# Why a fetch failed. ``missing``, ``unrecognised``, ``damaged`` and
# ``too_large`` come through unchanged from the reader, so one set of messages
# serves both.
REASON_DOWNLOAD: Final = "download_failed"
REASON_BLOCKED: Final = "blocked"
REASON_UNEXPECTED: Final = "unexpected"
REASON_WRITE: Final = "write_failed"

# Called as the transfer proceeds: bytes received so far, and the total the
# server announced, or ``None`` if it announced none.
Progress = Callable[[int, int | None], None]


class ZoneDataFetchError(Exception):
    """The dataset was not installed; the file at the fixed path is untouched."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"NWS zone data {reason}: {detail}")
        self.reason = reason


async def async_download_zone_data(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    url: str,
    sha256: str,
    *,
    allowed_hosts: Iterable[str] = (),
    user_agent: str | None = None,
    progress: Progress | None = None,
) -> str:
    """Fetch the dataset with hash ``sha256`` from ``url`` and install it.

    ``url`` and ``sha256`` are the caller's: this module does not know where
    datasets are published or which one a release wants, only how to refuse
    anything else. ``allowed_hosts`` are the hosts a redirect may lead to, in
    addition to the host in ``url``. ``session`` must verify certificates;
    Home Assistant's shared session does. Returns ``sha256``.
    """
    if sha256 not in KNOWN_ZONE_DATA:
        raise ZoneDataFetchError(
            REASON_UNRECOGNISED, "the dataset asked for is not a published one"
        )
    hosts = frozenset({_host_of(url), *allowed_hosts})
    async with _lock(hass):
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                stored = await _fetch(session, url, hosts, user_agent, progress)
        except (aiohttp.ClientError, TimeoutError) as err:
            # Not ``str(err)``: aiohttp puts the request URL in it, and the
            # caller already knows the URL.
            raise ZoneDataFetchError(REASON_DOWNLOAD, type(err).__name__) from err
        return await _install(hass, stored, sha256)


async def async_adopt_zone_data(hass: HomeAssistant) -> str:
    """Accept a dataset someone placed at the fixed path by hand. Returns its SHA-256.

    The same check as a download, in the other order: the file is already
    there, so it is verified where it lies and, if it passes, swapped into
    memory. If it fails it is left alone for the person to replace, and the
    error says what is wrong with it.
    """
    async with _lock(hass):
        try:
            _zones, digest = await hass.async_add_executor_job(
                read_verified, zone_data_path(hass)
            )
        except ZoneDataUnavailableError as err:
            raise ZoneDataFetchError(err.reason, str(err)) from err
        await _hand_over(hass)
        return digest


async def async_remove_zone_data(hass: HomeAssistant) -> bool:
    """Delete the dataset from the fixed path. Returns whether a file went.

    For when no entry uses local mode any more (the caller decides that):
    the file is 3.7 MB of this integration's own data and should not outlive
    its use. It waits for an install in progress, so it cannot remove half of
    one, and it takes a ``.part`` file left by an install that was cut short.
    It never raises: what calls it is the removal of an entry, which must
    not fail over a file. What could not be removed is logged and left.
    """
    async with _lock(hass):
        return await hass.async_add_executor_job(_remove, zone_data_path(hass))


def _remove(path: Path) -> bool:
    """Unlink the dataset and its ``.part`` file; a link itself, never its target."""
    removed = False
    for target in (path.with_name(path.name + ".part"), path):
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as err:
            _LOGGER.warning(
                "nws: the zone data file %s could not be removed (%s)",
                target.name,
                type(err).__name__,
            )
            continue
        removed = removed or target == path
    return removed


def _host_of(url: str) -> str:
    """The host in ``url``, or nothing if it cannot be read as an address."""
    try:
        return URL(url).host or ""
    except (TypeError, ValueError):
        return ""


def _check_address(url: str, hosts: frozenset[str]) -> None:
    """Refuse an address that is not plain https on a host the caller allowed."""
    try:
        address = URL(url)
        # Read here, inside the guard: the host is decoded when it is first
        # asked for, and a name that cannot be decoded raises only then.
        scheme, host = address.scheme, address.host
        credentials = address.user is not None or address.password is not None
        port = address.explicit_port
    except (TypeError, ValueError) as err:
        raise ZoneDataFetchError(REASON_BLOCKED, "not an address") from err
    if scheme != "https":
        raise ZoneDataFetchError(REASON_BLOCKED, "the address is not https")
    if credentials:
        raise ZoneDataFetchError(REASON_BLOCKED, "the address carries credentials")
    if port not in (None, 443):
        raise ZoneDataFetchError(REASON_BLOCKED, "the address names an unusual port")
    if not host or host not in hosts:
        raise ZoneDataFetchError(REASON_BLOCKED, "the host is not one that was allowed")


async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
    hosts: frozenset[str],
    user_agent: str | None,
    progress: Progress | None,
) -> bytes:
    # ``identity``: the file is already gzip. Asking for no transfer encoding
    # keeps the client from decompressing anything on the way in.
    headers = {"Accept-Encoding": "identity"}
    if user_agent:
        headers["User-Agent"] = user_agent
    for _hop in range(MAX_REDIRECTS + 1):
        _check_address(url, hosts)
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status in _REDIRECTS:
                location = response.headers.get("Location")
                if not location:
                    raise ZoneDataFetchError(
                        REASON_DOWNLOAD, "a redirect names no address"
                    )
                try:
                    url = str(URL(url).join(URL(location)))
                except (TypeError, ValueError) as err:
                    raise ZoneDataFetchError(
                        REASON_BLOCKED,
                        "a redirect names something that is not an address",
                    ) from err
                continue
            if response.status != 200:
                raise ZoneDataFetchError(REASON_DOWNLOAD, f"HTTP {response.status}")
            return await _read_body(response, progress)
    raise ZoneDataFetchError(REASON_BLOCKED, "too many redirects")


async def _read_body(
    response: aiohttp.ClientResponse, progress: Progress | None
) -> bytes:
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        # Refused before a byte of the body is read, so nothing is decompressed.
        raise ZoneDataFetchError(REASON_BLOCKED, "the response is transfer-compressed")
    announced = _announced_size(response)
    if announced is not None and announced > MAX_ZONE_DATA_BYTES:
        raise ZoneDataFetchError(REASON_TOO_LARGE, f"{announced} bytes announced")
    received = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK):
        received += chunk
        if len(received) > MAX_ZONE_DATA_BYTES:
            raise ZoneDataFetchError(
                REASON_TOO_LARGE, f"more than {MAX_ZONE_DATA_BYTES} bytes sent"
            )
        if progress is not None:
            progress(len(received), announced)
    if announced is not None and len(received) != announced:
        raise ZoneDataFetchError(
            REASON_DOWNLOAD, f"{announced} bytes announced, {len(received)} received"
        )
    return bytes(received)


def _announced_size(response: aiohttp.ClientResponse) -> int | None:
    try:
        size = int(response.headers["Content-Length"])
    except (KeyError, ValueError):
        return None
    return size if size >= 0 else None


async def _install(hass: HomeAssistant, stored: bytes, sha256: str) -> str:
    """Verify ``stored`` is the dataset asked for, put it in place, swap it in."""
    try:
        _zones, digest = await hass.async_add_executor_job(verify_zone_data, stored)
    except ZoneDataUnavailableError as err:
        raise ZoneDataFetchError(err.reason, str(err)) from err
    if digest != sha256:
        # A real, published dataset, but not the one this release asked for.
        # Taking it would let a download roll an install back to old zones.
        raise ZoneDataFetchError(
            REASON_UNEXPECTED, "a published dataset, but not the one asked for"
        )
    try:
        await hass.async_add_executor_job(_write_atomic, zone_data_path(hass), stored)
    except OSError as err:
        raise ZoneDataFetchError(REASON_WRITE, type(err).__name__) from err
    await _hand_over(hass)
    return digest


async def _hand_over(hass: HomeAssistant) -> None:
    """Give the file now in place to the entries that are running.

    It was verified a moment ago, so this fails only if something else
    changed the file in between. That still leaves as a fetch failure with
    its reason, like every other.
    """
    try:
        await async_reload_zone_data(hass)
    except ZoneDataUnavailableError as err:
        raise ZoneDataFetchError(err.reason, str(err)) from err


def _write_atomic(path: Path, stored: bytes) -> None:
    """Write ``stored`` to ``path`` so that ``path`` is never seen half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    # The .part file is made new every time and never opened as found. What
    # an earlier install left is removed first (a link itself, never what it
    # points to), then O_EXCL creates the file or fails: nothing that already
    # exists is written into. Opening what was there would write through a
    # symbolic link to wherever it points, and would wait for ever on a named
    # pipe with no reader. O_NOFOLLOW says the same of a link once more.
    # 0o600: created private, like the rest of ``.storage``.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    part.unlink(missing_ok=True)
    try:
        with os.fdopen(os.open(part, flags, 0o600), "wb") as f:
            f.write(stored)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, path)
    except OSError:
        part.unlink(missing_ok=True)
        raise


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    """One install at a time: two would race for the same ``.part`` file."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if _LOCK_KEY not in domain_data:
        domain_data[_LOCK_KEY] = asyncio.Lock()
    lock: asyncio.Lock = domain_data[_LOCK_KEY]
    return lock
