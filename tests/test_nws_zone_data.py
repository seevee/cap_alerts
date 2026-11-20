"""The NWS zone dataset on disk: one path, known files only, loaded on demand (#316).

``providers/nws_zone_data.py`` is what stands between a file in ``.storage``
and the resolver. These tests pin the three promises it makes:

* a file loads only if its SHA-256 is in the list this release carries, and
  each way it can fail says which way it failed;
* entries share one copy, read once, dropped when the last one lets go;
* an install that never asks for it never reads it. That last one is tested
  against a real entry setup, because it is a promise about the whole
  integration and not about this module.

The dataset here is three zones around New York Harbor, written the way
``scripts/build_zone_dataset.py`` writes the real one.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import os
import random
import tracemalloc
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cap_alerts.const import DOMAIN
from custom_components.cap_alerts.providers import nws_zone_data
from custom_components.cap_alerts.providers.nws_zone_data import (
    KNOWN_ZONE_DATA,
    REASON_DAMAGED,
    REASON_MISSING,
    REASON_TOO_LARGE,
    REASON_UNRECOGNISED,
    ZoneDataUnavailableError,
    async_get_zone_data,
    async_reload_zone_data,
    read_verified,
    verify_zone_data,
    zone_data_path,
)
from custom_components.cap_alerts.providers.nws_zones import ZoneSet

_READ = "custom_components.cap_alerts.providers.nws_zone_data.read_verified"

settings.register_profile("cap_alerts", deadline=None)
settings.load_profile("cap_alerts")


def _square(lon0: float, lat0: float, lon1: float, lat1: float) -> list[list[float]]:
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1]]


def _zone(code: str, layer: str, ring: list[list[float]]) -> dict[str, Any]:
    lons, lats = [p[0] for p in ring], [p[1] for p in ring]
    return {
        "code": code,
        "layer": layer,
        "bbox": [min(lons), min(lats), max(lons), max(lats)],
        "rings": [ring],
    }


_ZONES = [
    _zone("NYZ072", "forecast", _square(-74.1, 40.7, -73.9, 40.9)),
    _zone("NYC061", "county", _square(-74.1, 40.7, -73.9, 40.9)),
    _zone("ANZ338", "marine", _square(-73.95, 40.4, -73.6, 40.75)),
]


def _dataset(zones: list[dict[str, Any]] = _ZONES) -> bytes:
    """A gzipped line-per-zone dataset, as the builder writes it."""
    counts = {"zones": len(zones), "lines": len(zones) + 2}
    header = {"format": 2, **counts, "meta": {"epsilon_deg": 0.005}}
    body = b"".join(json.dumps(line).encode() + b"\n" for line in (header, *zones))
    footer = {"eof": True, **counts, "sha256": hashlib.sha256(body).hexdigest()}
    return gzip.compress(body + json.dumps(footer).encode() + b"\n", mtime=0)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def storage(hass, tmp_path):
    """Point Home Assistant's config directory at a scratch one."""
    hass.config.config_dir = str(tmp_path)
    path = zone_data_path(hass)
    path.parent.mkdir(parents=True)
    return path


@pytest.fixture
def known(monkeypatch):
    """Publish a dataset for the length of one test: add its hash to the list."""

    def _publish(data: bytes, release: str = "2026-04-16") -> bytes:
        monkeypatch.setitem(KNOWN_ZONE_DATA, _sha256(data), release)
        return data

    return _publish


# ---------------------------------------------------------------------------
# Where the file is, and which files are trusted
# ---------------------------------------------------------------------------


def test_the_dataset_has_one_fixed_path_in_storage(hass, storage):
    assert storage == Path(
        hass.config.config_dir, ".storage", "cap_alerts.nws_zones.jsonl.gz"
    )


def test_every_known_hash_is_a_sha256_with_a_release_date():
    assert KNOWN_ZONE_DATA
    for digest, release in KNOWN_ZONE_DATA.items():
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
        assert len(release) == 10 and release[4] == release[7] == "-"


def test_read_verified_loads_a_known_file(storage, known):
    data = known(_dataset())
    storage.write_bytes(data)
    zones, digest = read_verified(storage)
    assert isinstance(zones, ZoneSet)
    assert len(zones) == 3
    assert digest == _sha256(data)


def test_a_missing_file_says_so(storage):
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_MISSING
    assert str(storage) in str(raised.value)


def test_a_sound_file_that_was_never_published_is_refused(storage):
    """Well-formed is not enough: the hash has to be one this release knows."""
    storage.write_bytes(_dataset())
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_UNRECOGNISED


def test_an_unknown_file_is_refused_before_it_is_parsed(storage):
    storage.write_bytes(b"not gzip at all")
    with (
        patch.object(ZoneSet, "from_lines") as parse,
        pytest.raises(ZoneDataUnavailableError) as raised,
    ):
        read_verified(storage)
    assert raised.value.reason == REASON_UNRECOGNISED
    parse.assert_not_called()


@pytest.mark.parametrize(
    "broken",
    [
        b"not gzip at all",
        _dataset()[:-20],  # the gzip stream is cut short
        gzip.compress(
            b'{"format": 2, "zones": 1, "lines": 3}\n', mtime=0
        ),  # no end line
    ],
    ids=["not-gzip", "truncated-gzip", "no-end-of-file-line"],
)
def test_a_known_file_that_does_not_read_back_whole_is_damaged(storage, known, broken):
    """Only reachable if a listed hash is wrong or the list is; still must not load."""
    storage.write_bytes(known(broken))
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_DAMAGED


# ---------------------------------------------------------------------------
# A hostile file
#
# Assume whatever is at the fixed path, or arrives in a download, was made by
# someone who wants to crash Home Assistant, exhaust its memory or get wrong
# data loaded. The order of the checks is the defence: size, then hash, and
# only then decompression and parsing, each under its own ceiling.
# ---------------------------------------------------------------------------


def test_bytes_that_are_not_a_published_dataset_are_never_decompressed(storage):
    """An attacker's bytes stop at the hash. They do not reach zlib or the parser."""
    hostile = gzip.compress(b"anything at all" * 1000, mtime=0)
    with (
        patch.object(nws_zone_data.gzip, "GzipFile") as decompress,
        patch.object(ZoneSet, "from_lines") as parse,
        pytest.raises(ZoneDataUnavailableError) as raised,
    ):
        verify_zone_data(hostile)
    assert raised.value.reason == REASON_UNRECOGNISED
    decompress.assert_not_called()
    parse.assert_not_called()


def test_bytes_over_the_size_ceiling_are_refused_before_they_are_hashed(monkeypatch):
    monkeypatch.setattr(nws_zone_data, "MAX_ZONE_DATA_BYTES", 1000)
    with (
        patch.object(nws_zone_data.hashlib, "sha256") as digest,
        pytest.raises(ZoneDataUnavailableError) as raised,
    ):
        verify_zone_data(b"x" * 1001)
    assert raised.value.reason == REASON_TOO_LARGE
    digest.assert_not_called()


def test_a_file_over_the_size_ceiling_is_not_read_whole(storage, monkeypatch):
    monkeypatch.setattr(nws_zone_data, "MAX_ZONE_DATA_BYTES", 1000)
    storage.write_bytes(b"x" * 50_000)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_TOO_LARGE
    assert "1001 bytes" in str(raised.value)  # the ceiling plus one, and no more


def _bomb(megabytes: int, block: bytes) -> bytes:
    """A small gzip file that expands to ``megabytes`` MB, built without holding them."""
    packer = zlib.compressobj(9, zlib.DEFLATED, 31)
    packed = b"".join(packer.compress(block * (1 << 20)) for _ in range(megabytes))
    return packed + packer.flush()


def test_a_decompression_bomb_of_one_endless_line_is_stopped_at_the_line_ceiling(known):
    """300 MB of zeros in 300 kB. No newline, so a naive reader builds one line."""
    bomb = known(_bomb(300, b"\0"))
    assert len(bomb) < 1_000_000
    tracemalloc.start()
    try:
        with pytest.raises(ZoneDataUnavailableError) as raised:
            verify_zone_data(bomb)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert raised.value.reason == REASON_DAMAGED
    assert "a line is longer than" in str(raised.value)
    assert peak < 16 * 1024 * 1024  # nowhere near the 300 MB it claims


def test_a_decompression_bomb_of_many_lines_is_stopped_at_the_size_ceiling(
    known, monkeypatch
):
    monkeypatch.setattr(nws_zone_data, "MAX_DATASET_BYTES", 300)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        verify_zone_data(known(_dataset()))
    assert raised.value.reason == REASON_DAMAGED
    assert "expands to more than 300 bytes" in str(raised.value)


def test_a_file_whose_gzip_checksum_is_wrong_is_damaged(known):
    """The trailer's CRC is only checked at the end, so the read must reach it."""
    good = _dataset()
    corrupt = bytearray(good)
    corrupt[-8] ^= 0xFF  # first byte of the CRC-32
    with pytest.raises(ZoneDataUnavailableError) as raised:
        verify_zone_data(known(bytes(corrupt)))
    assert raised.value.reason == REASON_DAMAGED


def test_a_file_whose_gzip_length_is_wrong_is_damaged(known):
    corrupt = bytearray(_dataset())
    corrupt[-1] ^= 0xFF  # last byte of the stored length
    with pytest.raises(ZoneDataUnavailableError) as raised:
        verify_zone_data(known(bytes(corrupt)))
    assert raised.value.reason == REASON_DAMAGED


def test_a_second_gzip_member_after_the_dataset_is_damaged(known):
    """gzip reads concatenated members as one stream; the end-of-file line stops it."""
    smuggled = _dataset() + gzip.compress(b'{"code": "NYZ999"}\n', mtime=0)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        verify_zone_data(known(smuggled))
    assert raised.value.reason == REASON_DAMAGED


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_pipe_left_at_the_fixed_path_is_refused_without_waiting(storage):
    """Opening a pipe for reading normally blocks until something writes to it."""
    os.mkfifo(storage)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_UNRECOGNISED


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs /dev/zero")
def test_a_link_to_an_endless_device_is_refused_unread(storage):
    storage.symlink_to("/dev/zero")
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_UNRECOGNISED


def test_a_path_that_cannot_be_opened_is_refused(storage):
    """Here the storage directory is itself a file, so the path cannot exist."""
    storage.parent.rmdir()
    storage.parent.write_bytes(b"not a directory")
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_DAMAGED


def test_a_read_error_is_refused(storage, known):
    storage.write_bytes(known(_dataset()))
    with (
        patch.object(nws_zone_data.os, "fstat", side_effect=OSError("I/O error")),
        pytest.raises(ZoneDataUnavailableError) as raised,
    ):
        read_verified(storage)
    assert raised.value.reason == REASON_DAMAGED


def test_a_file_that_fails_while_being_read_is_refused(storage, known, monkeypatch):
    """A failing disk or SD card reports the error on the read, not on the open."""
    storage.write_bytes(known(_dataset()))

    class Unreadable(io.BytesIO):
        def read(self, *args: Any) -> bytes:
            raise OSError("I/O error")

    def wrap(descriptor: int, mode: str) -> Unreadable:
        os.close(descriptor)
        return Unreadable()

    monkeypatch.setattr(os, "fdopen", wrap)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    monkeypatch.undo()
    assert raised.value.reason == REASON_DAMAGED


def test_a_directory_at_the_fixed_path_is_refused(storage):
    storage.mkdir()
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    assert raised.value.reason == REASON_UNRECOGNISED


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
def test_a_file_that_cannot_be_wrapped_is_refused_and_its_descriptor_closed(
    storage, known, monkeypatch
):
    storage.write_bytes(known(_dataset()))
    before = len(os.listdir("/proc/self/fd"))

    def refuse(*args, **kwargs):
        raise OSError("no file object for this descriptor")

    monkeypatch.setattr(os, "fdopen", refuse)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        read_verified(storage)
    monkeypatch.undo()
    assert raised.value.reason == REASON_DAMAGED
    assert len(os.listdir("/proc/self/fd")) == before


def _open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
@pytest.mark.parametrize("kind", ["directory", "pipe", "missing", "unknown file"])
def test_a_refused_path_leaves_no_descriptor_open(storage, kind):
    """A refusal is repeated on every retry, so it must not cost a descriptor.

    A directory at the fixed path did: the file object refused it and left
    the descriptor open. Two hundred attempts left two hundred open.
    """
    if kind == "directory":
        storage.mkdir()
    elif kind == "pipe":
        os.mkfifo(storage)
    elif kind == "unknown file":
        storage.write_bytes(_dataset())
    before = _open_descriptors()
    for _ in range(50):
        with pytest.raises(ZoneDataUnavailableError):
            read_verified(storage)
    assert _open_descriptors() == before


@given(data=st.data())
def test_no_damage_to_a_listed_file_gets_past_as_anything_but_a_refusal(data):
    """The worst case: the hash list is wrong and hostile bytes reach zlib.

    Each damaged file is put on the list, so the hash check passes and the
    decompressor and parser see it. Whatever the bytes, the result is a
    dataset or ZoneDataUnavailableError. No other exception.
    """
    stored = bytearray(_dataset())
    for _ in range(data.draw(st.integers(1, 6))):
        position = data.draw(st.integers(0, len(stored) - 1))
        action = data.draw(st.sampled_from(["flip", "drop", "insert"]))
        if action == "flip":
            stored[position] = data.draw(st.integers(0, 255))
        elif action == "drop" and len(stored) > 1:
            del stored[position]
        else:
            stored.insert(position, data.draw(st.integers(0, 255)))
    damaged = bytes(stored)
    with patch.dict(KNOWN_ZONE_DATA, {_sha256(damaged): "2026-04-16"}):
        try:
            verify_zone_data(damaged)
        except ZoneDataUnavailableError:
            pass


@given(stored=st.binary(max_size=4096))
def test_arbitrary_bytes_on_the_list_load_or_are_refused(stored):
    with patch.dict(KNOWN_ZONE_DATA, {_sha256(stored): "2026-04-16"}):
        try:
            verify_zone_data(stored)
        except ZoneDataUnavailableError:
            pass


# ---------------------------------------------------------------------------
# One shared copy, read on demand, dropped when unused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_loaded_until_someone_asks(hass, storage, known):
    storage.write_bytes(known(_dataset()))
    with patch(_READ, wraps=read_verified) as read:
        zone_data = async_get_zone_data(hass)
        assert not zone_data.loaded
        assert zone_data.sha256 is None
        assert zone_data.release_date is None
        read.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_loads_the_dataset_and_reports_what_it_is(hass, storage, known):
    data = known(_dataset(), "2026-04-16")
    storage.write_bytes(data)
    zone_data = async_get_zone_data(hass)
    zones = await zone_data.async_acquire("entry-1")
    assert len(zones) == 3
    assert zone_data.loaded
    assert zone_data.sha256 == _sha256(data)
    assert zone_data.release_date == "2026-04-16"


@pytest.mark.asyncio
async def test_the_instance_has_one_zone_data_object(hass):
    assert async_get_zone_data(hass) is async_get_zone_data(hass)


@pytest.mark.asyncio
async def test_two_entries_share_one_copy_read_once(hass, storage, known):
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    with patch(_READ, wraps=read_verified) as read:
        first = await zone_data.async_acquire("entry-1")
        second = await zone_data.async_acquire("entry-2")
    assert first is second
    assert read.call_count == 1


@pytest.mark.asyncio
async def test_two_entries_starting_together_read_the_file_once(hass, storage, known):
    """The second entry must wait for the first one's read, not start its own.

    The test harness runs an executor job to completion before anything else
    gets a turn, which would hide a missing lock. The stand-in here gives up
    its turn in the middle of the read, as a real read off the event loop
    does, so that the second entry runs while the first is still reading.
    """
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    reads = 0

    async def a_read_that_takes_time(func, *args):
        nonlocal reads
        reads += 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return func(*args)

    with patch.object(hass, "async_add_executor_job", new=a_read_that_takes_time):
        first, second = await asyncio.gather(
            zone_data.async_acquire("entry-1"), zone_data.async_acquire("entry-2")
        )
    assert first is second
    assert reads == 1


@pytest.mark.asyncio
async def test_the_file_is_read_in_the_executor(hass, storage, known):
    storage.write_bytes(known(_dataset()))
    with patch.object(
        hass, "async_add_executor_job", wraps=hass.async_add_executor_job
    ) as executor:
        await async_get_zone_data(hass).async_acquire("entry-1")
    executor.assert_called_once_with(read_verified, storage)


@pytest.mark.asyncio
async def test_the_dataset_is_kept_while_any_entry_holds_it(hass, storage, known):
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    await zone_data.async_acquire("entry-1")
    await zone_data.async_acquire("entry-2")
    zone_data.release("entry-1")
    assert zone_data.loaded


@pytest.mark.asyncio
async def test_the_dataset_is_dropped_when_the_last_entry_lets_go(hass, storage, known):
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    await zone_data.async_acquire("entry-1")
    await zone_data.async_acquire("entry-2")
    zone_data.release("entry-1")
    zone_data.release("entry-2")
    assert not zone_data.loaded
    assert zone_data.sha256 is None


@pytest.mark.asyncio
async def test_releasing_twice_or_without_holding_is_harmless(hass, storage, known):
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    zone_data.release("never-held")
    await zone_data.async_acquire("entry-1")
    zone_data.release("entry-1")
    zone_data.release("entry-1")
    assert not zone_data.loaded


@pytest.mark.asyncio
async def test_a_dropped_dataset_is_read_again_on_the_next_request(
    hass, storage, known
):
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    with patch(_READ, wraps=read_verified) as read:
        await zone_data.async_acquire("entry-1")
        zone_data.release("entry-1")
        await zone_data.async_acquire("entry-1")
    assert read.call_count == 2
    assert zone_data.loaded


@pytest.mark.asyncio
async def test_a_failed_load_holds_nothing_and_can_be_retried(hass, storage, known):
    """The file arriving later is the normal case: fetch it, then ask again."""
    zone_data = async_get_zone_data(hass)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        await zone_data.async_acquire("entry-1")
    assert raised.value.reason == REASON_MISSING
    assert not zone_data.loaded

    storage.write_bytes(known(_dataset()))
    assert len(await zone_data.async_acquire("entry-1")) == 3


@pytest.mark.asyncio
async def test_an_upgrade_keeps_an_older_published_file_working(hass, storage, known):
    """The list carries every published hash, so last year's file still loads."""
    older = known(_dataset(_ZONES[:2]), "2025-09-10")
    known(_dataset(), "2026-04-16")
    storage.write_bytes(older)
    zone_data = async_get_zone_data(hass)
    assert len(await zone_data.async_acquire("entry-1")) == 2
    assert zone_data.release_date == "2025-09-10"


# ---------------------------------------------------------------------------
# The one check, and replacing the file while it is in use
# ---------------------------------------------------------------------------


def test_bytes_are_verified_the_same_way_as_a_file(known):
    """A download or a hand-supplied file is checked before it is written."""
    data = known(_dataset())
    zones, digest = verify_zone_data(data)
    assert len(zones) == 3
    assert digest == _sha256(data)
    with pytest.raises(ZoneDataUnavailableError) as raised:
        verify_zone_data(_dataset(_ZONES[:1]))
    assert raised.value.reason == REASON_UNRECOGNISED


@pytest.mark.asyncio
async def test_reload_swaps_in_a_replaced_file_while_entries_hold_it(
    hass, storage, known
):
    """Reloading entries one by one would hand each the old set; this does not."""
    storage.write_bytes(known(_dataset(_ZONES[:2]), "2025-09-10"))
    zone_data = async_get_zone_data(hass)
    old = await zone_data.async_acquire("entry-1")
    await zone_data.async_acquire("entry-2")

    storage.write_bytes(known(_dataset(), "2026-04-16"))
    assert await zone_data.async_reload()

    new = await zone_data.async_acquire("entry-1")
    assert new is not old
    assert len(new) == 3
    assert await zone_data.async_acquire("entry-2") is new
    assert zone_data.release_date == "2026-04-16"


@pytest.mark.asyncio
async def test_an_entry_that_asks_during_a_reload_gets_the_new_file(
    hass, storage, known
):
    """An entry starting while a replaced file is being read must wait for it.
    Handed the set in memory at that moment, it would run on the old file."""
    storage.write_bytes(known(_dataset(_ZONES[:2]), "2025-09-10"))
    zone_data = async_get_zone_data(hass)
    old = await zone_data.async_acquire("entry-1")
    storage.write_bytes(known(_dataset(), "2026-04-16"))

    async def a_read_that_takes_time(func, *args):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return func(*args)

    with patch.object(hass, "async_add_executor_job", new=a_read_that_takes_time):
        reloaded, late = await asyncio.gather(
            zone_data.async_reload(), zone_data.async_acquire("entry-2")
        )
    assert reloaded is True
    assert late is not old
    assert len(late) == 3


@pytest.mark.asyncio
async def test_the_module_level_reload_swaps_the_file_for_its_holders(
    hass, storage, known
):
    """This is the function a download step calls after it replaces the file."""
    storage.write_bytes(known(_dataset(_ZONES[:2]), "2025-09-10"))
    zone_data = async_get_zone_data(hass)
    old = await zone_data.async_acquire("entry-1")

    storage.write_bytes(known(_dataset(), "2026-04-16"))
    assert await async_reload_zone_data(hass) is True

    new = await zone_data.async_acquire("entry-1")
    assert new is not old
    assert len(new) == 3
    assert zone_data.release_date == "2026-04-16"


@pytest.mark.asyncio
async def test_the_module_level_reload_creates_nothing_on_an_install_that_never_asked(
    hass, storage, known
):
    """An install with no local-matching entry has no ZoneData object, and a
    reload must not create one or read the file."""
    storage.write_bytes(known(_dataset()))
    with patch(_READ, wraps=read_verified) as read:
        assert await async_reload_zone_data(hass) is False
    read.assert_not_called()
    assert "nws_zone_data" not in hass.data.get("cap_alerts", {})


@pytest.mark.asyncio
async def test_a_reload_keeps_nothing_if_the_last_holder_lets_go_meanwhile(
    hass, storage, known
):
    """The file is read off the event loop, and an entry can be unloaded while
    that is happening. The set read for it must not stay in memory unused."""
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    await zone_data.async_acquire("entry-1")

    async def read_while_the_holder_leaves(func, *args):
        """Stands in for the executor: the holder lets go before the read returns."""
        result = func(*args)
        zone_data.release("entry-1")
        return result

    with patch.object(hass, "async_add_executor_job", new=read_while_the_holder_leaves):
        assert await zone_data.async_reload() is False
    assert not zone_data.loaded
    assert zone_data.sha256 is None


@pytest.mark.asyncio
async def test_reload_reads_nothing_when_nobody_holds_the_dataset(hass, storage, known):
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    with patch(_READ, wraps=read_verified) as read:
        assert not await zone_data.async_reload()
    read.assert_not_called()
    assert not zone_data.loaded


@pytest.mark.asyncio
async def test_a_bad_replacement_leaves_the_working_set_in_place(hass, storage, known):
    data = known(_dataset())
    storage.write_bytes(data)
    zone_data = async_get_zone_data(hass)
    held = await zone_data.async_acquire("entry-1")

    storage.write_bytes(b"something else entirely")
    with pytest.raises(ZoneDataUnavailableError) as raised:
        await zone_data.async_reload()
    assert raised.value.reason == REASON_UNRECOGNISED
    assert await zone_data.async_acquire("entry-1") is held
    assert zone_data.sha256 == _sha256(data)


# ---------------------------------------------------------------------------
# Off means off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_order_of_acquire_release_and_reload_keeps_the_books_straight(
    hass, storage, known
):
    """Two thousand operations in a seeded random order, by four entries.

    After every one: the dataset is in memory exactly when somebody holds it,
    every holder is handed the same object, a reload loads exactly when
    somebody holds it, and the file is read only when it has to be.
    """
    storage.write_bytes(known(_dataset()))
    zone_data = async_get_zone_data(hass)
    rng = random.Random(316)
    holders: set[str] = set()
    with patch(_READ, wraps=read_verified) as read:
        for _ in range(2000):
            reads = read.call_count
            operation = rng.choice(
                ("acquire", "acquire", "release", "reload", "together")
            )
            name = f"entry-{rng.randrange(4)}"
            if operation == "acquire":
                was_loaded = zone_data.loaded
                await zone_data.async_acquire(name)
                holders.add(name)
                assert read.call_count == reads + (0 if was_loaded else 1)
            elif operation == "release":
                zone_data.release(name)
                holders.discard(name)
                assert read.call_count == reads
            elif operation == "reload":
                assert await zone_data.async_reload() is bool(holders)
                assert read.call_count == reads + (1 if holders else 0)
            else:
                was_loaded = zone_data.loaded
                first, second = await asyncio.gather(
                    zone_data.async_acquire("entry-0"),
                    zone_data.async_acquire("entry-1"),
                )
                assert first is second
                holders |= {"entry-0", "entry-1"}
                assert read.call_count == reads + (0 if was_loaded else 1)
            assert zone_data.loaded is bool(holders)
            assert (zone_data.sha256 is not None) is bool(holders)
            if holders:
                current = await zone_data.async_acquire(min(holders))
                for holder in holders:
                    assert await zone_data.async_acquire(holder) is current


@pytest.mark.asyncio
async def test_an_entry_that_does_not_use_local_matching_never_touches_the_dataset(
    hass, aioclient_mock, enable_custom_integrations, storage, known
):
    """A zone-scoped NWS entry sets up, polls and unloads; the file stays shut.

    The dataset is present and valid, so nothing stops it loading except the
    integration not asking. No ``ZoneData`` object is even created.
    """
    storage.write_bytes(known(_dataset()))
    aioclient_mock.get(
        "https://api.weather.gov/alerts/active?zone=NYZ072", json={"features": []}
    )
    entry = MockConfigEntry(domain=DOMAIN, data={"provider": "nws", "zone": "NYZ072"})
    entry.add_to_hass(hass)
    with patch(_READ, side_effect=AssertionError("the dataset was read")) as read:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    read.assert_not_called()
    assert nws_zone_data._DATA_KEY not in hass.data[DOMAIN]
