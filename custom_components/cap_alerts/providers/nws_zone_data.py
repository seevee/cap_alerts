"""The NWS zone dataset on disk: where it lives, which files are trusted, when it loads.

``providers/nws_zones.py`` turns a dataset into a ``ZoneSet`` and knows nothing
about Home Assistant. This module is the other half: it finds the file at one
fixed path, checks that it is a dataset this release knows, and hands out one
shared ``ZoneSet`` to the entries that use local matching (issue #316).

How the file gets to that path is not decided here. A download step, a file
placed by hand and a package that installs it all end at the same place, and
all of them pass through the same check.

Three rules shape it:

* **Only known files load.** ``KNOWN_ZONE_DATA`` lists the SHA-256 of every
  dataset ever published, not only the newest, so a file that was valid under
  an earlier release stays valid after an upgrade. A file whose hash is not in
  the list is refused before a byte of it is parsed. The hash is taken over
  the ``.gz`` exactly as stored, which is the file a release publishes.
* **Off means off.** Nothing here runs at import or at setup. The file is read
  the first time an entry asks for it and not before, so an install with no
  local-matching entry pays nothing in memory or CPU, and never opens the
  file. On small hardware that is the difference between the feature being
  optional and being a tax.
* **One copy, released when unused.** Entries share a single ``ZoneSet``.
  Each holds it under its own name; when the last one lets go
  the set is dropped, and a later request reads the file again. When the file
  is replaced while entries hold it, ``async_reload`` swaps the shared copy.

Reading and parsing use CPU time and touch the disk, so they run in the
executor, never on the event loop.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import os
import stat
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import STORAGE_DIR

from ..const import DOMAIN
from .nws_zones import MAX_DATASET_BYTES, MAX_LINE_BYTES, ZoneDataError, ZoneSet

# Beside the integration's other files in ``.storage``, under its own prefix.
ZONE_DATA_FILENAME: Final = f"{DOMAIN}.nws_zones.jsonl.gz"

# SHA-256 of each published ``nws_zones.jsonl.gz`` -> the NWS release it was
# built from. Append only: removing an entry strands every install still
# running on that file. The builder's manifest gives the value as
# ``gzip_sha256`` (``scripts/build_zone_dataset.py --gzip``).
KNOWN_ZONE_DATA: Final[dict[str, str]] = {
    # 16 April 2026 shapefiles, epsilon 0.005 deg (0.02 for Alaska), builder
    # revision 3, build_id 90bad5aff2af3c05, 11,651 zones.
    "c985ed4fbfe4a320a4f3301576b5d6d7fa6f98e1e82e59384a1861b91ae5ee58": "2026-04-16",
}

# The published ``.gz`` is about 3 MB. Nothing larger than this is read from
# the disk or accepted from a download, so a wrong or hostile file cannot ask
# for more memory than this before it is refused.
MAX_ZONE_DATA_BYTES: Final = 16 * 1024 * 1024

_DATA_KEY: Final = "nws_zone_data"

# Why a dataset could not be used. Stable strings: flows and repairs key
# their messages on them.
REASON_MISSING: Final = "missing"
REASON_UNRECOGNISED: Final = "unrecognised"
REASON_DAMAGED: Final = "damaged"
REASON_TOO_LARGE: Final = "too_large"


class ZoneDataUnavailableError(Exception):
    """There is no usable zone dataset at the fixed path.

    ``reason`` says what is wrong, because each has a different remedy:
    nothing there (fetch or supply one), a file this release does not know
    (it is not a published dataset, or it is newer than the code), a known
    file that does not read back whole (replace it), or something far too
    large to be a dataset at all.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"NWS zone data {reason}: {detail}")
        self.reason = reason


def zone_data_path(hass: HomeAssistant) -> Path:
    """The one place the dataset is looked for."""
    return Path(hass.config.path(STORAGE_DIR, ZONE_DATA_FILENAME))


def verify_zone_data(stored: bytes) -> tuple[ZoneSet, str]:
    """Parse ``stored`` if, and only if, it is a published dataset.

    The one check every route to the fixed path goes through: the file
    already on disk, a download before it is written, a file supplied by
    hand. ``stored`` is the ``.gz`` exactly as published. The hash and the
    parse use the same bytes, so what was checked is what was loaded.
    Blocking: call it in the executor.

    Written on the assumption that ``stored`` may be hostile. The order is
    what makes that safe:

    1. Size. More than ``MAX_ZONE_DATA_BYTES`` is refused outright.
    2. SHA-256 against ``KNOWN_ZONE_DATA``. Bytes that are not a published
       dataset stop here. They are never decompressed and never parsed, so
       nothing an attacker can craft reaches zlib or the JSON parser.
    3. Decompression, a line at a time, under a ceiling on each line and on
       the total. A gzip stream can expand a thousandfold; this one cannot
       take more than ``MAX_DATASET_BYTES`` whatever it claims to contain.
    4. ``ZoneSet.from_lines``, which checks every field and the gzip trailer
       (CRC and length) by reading to the end.

    Steps 3 and 4 can only be reached by a file whose hash is on the list,
    and they are still written to refuse a bad one: the list could be wrong.
    """
    if len(stored) > MAX_ZONE_DATA_BYTES:
        raise ZoneDataUnavailableError(
            REASON_TOO_LARGE, f"{len(stored)} bytes, more than {MAX_ZONE_DATA_BYTES}"
        )
    digest = hashlib.sha256(stored).hexdigest()
    if digest not in KNOWN_ZONE_DATA:
        raise ZoneDataUnavailableError(
            REASON_UNRECOGNISED, f"SHA-256 {digest} is not a published dataset"
        )
    try:
        zones = ZoneSet.from_lines(_bounded_lines(stored))
    except (ZoneDataError, OSError, EOFError, zlib.error) as err:
        # OSError and EOFError are what gzip raises for a stream that is not
        # gzip, is cut short, or fails its CRC or length check. zlib.error is
        # what a corrupt deflate stream raises, and it is not an OSError.
        raise ZoneDataUnavailableError(REASON_DAMAGED, str(err)) from err
    return zones, digest


def _bounded_lines(stored: bytes) -> Iterator[bytes]:
    """The lines of a gzipped dataset, never holding more than one bounded line.

    ``readline`` is given a limit, so a stream with no newline in it cannot
    make this build one enormous line: it gets at most the ceiling plus one
    byte, which is enough to see that the ceiling was passed.
    """
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(stored), mode="rb") as stream:
        while line := stream.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES:
                raise ZoneDataError(f"a line is longer than {MAX_LINE_BYTES} bytes")
            total += len(line)
            if total > MAX_DATASET_BYTES:
                raise ZoneDataError(
                    f"the dataset expands to more than {MAX_DATASET_BYTES} bytes"
                )
            yield line


def read_verified(path: Path) -> tuple[ZoneSet, str]:
    """Read and verify the dataset at ``path``. Blocking: call it in the executor.

    The file is opened once and everything is done through that one handle,
    so what is checked is what is read. It must be an ordinary file. A pipe
    or a link to a device left at the fixed path would otherwise block
    forever or be read without end, so it is opened without waiting and
    refused once it is seen not to be a file. No more than the ceiling plus
    one byte is ever read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as err:
        raise ZoneDataUnavailableError(REASON_MISSING, str(path)) from err
    except OSError as err:
        raise ZoneDataUnavailableError(REASON_DAMAGED, type(err).__name__) from err
    # The descriptor is checked before a file object takes ownership of it.
    # ``os.fdopen`` raises for a directory and then leaves the descriptor
    # open, so a directory at the fixed path would cost one descriptor on
    # every attempt until the process had none left.
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ZoneDataUnavailableError(
                REASON_UNRECOGNISED, f"{path} is not an ordinary file"
            )
        handle = os.fdopen(descriptor, "rb")
    except ZoneDataUnavailableError:
        os.close(descriptor)
        raise
    except OSError as err:
        os.close(descriptor)
        raise ZoneDataUnavailableError(REASON_DAMAGED, type(err).__name__) from err
    try:
        with handle:
            stored = handle.read(MAX_ZONE_DATA_BYTES + 1)
    except OSError as err:
        raise ZoneDataUnavailableError(REASON_DAMAGED, type(err).__name__) from err
    return verify_zone_data(stored)


class ZoneData:
    """The shared dataset for one Home Assistant instance.

    Cheap to create: it holds no data until the first ``async_acquire``.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._zones: ZoneSet | None = None
        self._sha256: str | None = None
        self._holders: set[str] = set()
        # Two entries starting together must not read the file twice.
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        """Whether the dataset is in memory right now."""
        return self._zones is not None

    @property
    def sha256(self) -> str | None:
        """The hash of the file in use, for diagnostics; ``None`` when not loaded."""
        return self._sha256

    @property
    def release_date(self) -> str | None:
        """The NWS release the file in use was built from; ``None`` when not loaded."""
        return KNOWN_ZONE_DATA[self._sha256] if self._sha256 else None

    async def async_acquire(self, holder: str) -> ZoneSet:
        """Return the dataset, reading it first if nobody holds it yet.

        ``holder`` names who is asking, a config entry ID in practice. Pair
        every call with ``release`` using the same name. Raises
        ``ZoneDataUnavailableError`` and records nothing if the file cannot
        be used, so a later attempt starts clean.
        """
        async with self._lock:
            if self._zones is not None:
                self._holders.add(holder)
                return self._zones
            zones, digest = await self._hass.async_add_executor_job(
                read_verified, zone_data_path(self._hass)
            )
            self._zones, self._sha256 = zones, digest
            self._holders.add(holder)
            return zones

    async def async_reload(self) -> bool:
        """Read the file again after it has been replaced. True if a set was loaded.

        Releasing and acquiring is not enough to pick up a new file: while any
        entry still holds the dataset it stays in memory, so entries reloaded
        one after another would each be handed the old one. This swaps the
        shared set in place; holders get the new one from their next
        ``async_acquire``.

        With nobody holding the dataset there is nothing to swap, and nothing
        is read: the next ``async_acquire`` will read the new file anyway. If
        the new file cannot be used this raises and the set in memory stays as
        it was, so a bad replacement does not take a working install down.
        """
        async with self._lock:
            if not self._holders:
                return False
            zones, digest = await self._hass.async_add_executor_job(
                read_verified, zone_data_path(self._hass)
            )
            if not self._holders:
                # The last holder let go while the file was being read.
                # ``release`` has already dropped the set, and keeping the
                # new one would hold it in memory with nobody using it.
                return False
            self._zones, self._sha256 = zones, digest
            return True

    @callback
    def release(self, holder: str) -> None:
        """Let go of the dataset; drop it from memory when no holder is left."""
        self._holders.discard(holder)
        if not self._holders:
            self._zones = None
            self._sha256 = None


@callback
def async_get_zone_data(hass: HomeAssistant) -> ZoneData:
    """The instance-wide ``ZoneData``, created on first use.

    Calling this reads nothing. Only code on the local-matching path should
    call it at all: an install that never does has no ``ZoneData`` object.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if _DATA_KEY not in domain_data:
        domain_data[_DATA_KEY] = ZoneData(hass)
    zone_data: ZoneData = domain_data[_DATA_KEY]
    return zone_data


async def async_reload_zone_data(hass: HomeAssistant) -> bool:
    """Swap in a replaced file for the entries that hold the dataset.

    For whatever just put a new file at the fixed path. Unlike
    ``async_get_zone_data`` it creates nothing: on an install where nobody
    uses local matching there is no ``ZoneData`` object, and there still is
    none afterwards.
    """
    zone_data: ZoneData | None = hass.data.get(DOMAIN, {}).get(_DATA_KEY)
    return await zone_data.async_reload() if zone_data is not None else False
