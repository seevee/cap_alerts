"""Putting the NWS zone dataset on disk: fetch, verify, write, swap (#316).

``providers/nws_zone_fetch.py`` is the one path by which a dataset arrives.
What matters about it is mostly what it refuses to do:

* it accepts exactly the dataset it was asked for, by hash, and nothing else;
* it never writes a file it has not verified, so a bad download cannot replace
  a working dataset;
* it never leaves the fixed path half-written;
* it stops reading a response that is too large, whatever the headers said;
* it goes only where it was sent: https, allowed hosts, a few redirects;
* every failure says which kind it was and leaves things as they were.

The second half of this file assumes the server is hostile.

The dataset here is three zones around New York Harbor, written the way
``scripts/build_zone_dataset.py`` writes the real one.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import stat
import time
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.cap_alerts.const import DOMAIN
from custom_components.cap_alerts.providers import nws_zone_fetch
from custom_components.cap_alerts.providers.nws_zone_data import (
    KNOWN_ZONE_DATA,
    REASON_DAMAGED,
    REASON_MISSING,
    REASON_TOO_LARGE,
    REASON_UNRECOGNISED,
    ZoneDataUnavailableError,
    async_get_zone_data,
    async_reload_zone_data,
    zone_data_path,
)
from custom_components.cap_alerts.providers.nws_zone_fetch import (
    REASON_BLOCKED,
    REASON_DOWNLOAD,
    REASON_UNEXPECTED,
    REASON_WRITE,
    ZoneDataFetchError,
    async_adopt_zone_data,
    async_download_zone_data,
    async_remove_zone_data,
)

_URL = "https://example.invalid/releases/download/v0.9.0/nws_zones.jsonl.gz"
_AGENT = "HomeAssistant-CAPAlerts/test"


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
    return zone_data_path(hass)


@pytest.fixture
def known(monkeypatch):
    """Publish a dataset for the length of one test: add its hash to the list."""

    def _publish(data: bytes, release: str = "2026-04-16") -> bytes:
        monkeypatch.setitem(KNOWN_ZONE_DATA, _sha256(data), release)
        return data

    return _publish


_WANTED = _dataset()


@pytest.fixture(autouse=True)
def wanted(known):
    """The dataset this release asks for is a published one."""
    return known(_WANTED)


async def _download(
    hass, *, url: str = _URL, sha256: str | None = None, **kwargs: Any
) -> str:
    return await async_download_zone_data(
        hass,
        async_get_clientsession(hass),
        url,
        sha256 or _sha256(_WANTED),
        user_agent=_AGENT,
        **kwargs,
    )


def _leftovers(storage) -> list[str]:
    """Everything in the storage directory except the dataset itself."""
    if not storage.parent.exists():
        return []
    return sorted(p.name for p in storage.parent.iterdir() if p != storage)


# ---------------------------------------------------------------------------
# A good download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_download_lands_at_the_fixed_path_and_reports_its_hash(
    hass, aioclient_mock, storage, known
):
    data = known(_dataset())
    aioclient_mock.get(_URL, content=data)
    assert await _download(hass) == _sha256(data)
    assert storage.read_bytes() == data
    assert _leftovers(storage) == []


@pytest.mark.asyncio
async def test_the_request_is_made_once_and_identifies_the_integration(
    hass, aioclient_mock, storage, known
):
    aioclient_mock.get(_URL, content=known(_dataset()))
    await _download(hass)
    assert aioclient_mock.call_count == 1
    _method, url, _data, headers = aioclient_mock.mock_calls[0]
    assert str(url) == _URL
    assert headers["User-Agent"] == _AGENT


@pytest.mark.asyncio
async def test_progress_reports_bytes_received_against_the_announced_total(
    hass, aioclient_mock, storage, known
):
    data = known(_dataset())
    aioclient_mock.get(_URL, content=data, headers={"Content-Length": str(len(data))})
    seen: list[tuple[int, int | None]] = []
    with patch.object(nws_zone_fetch, "_CHUNK", 64):
        await _download(hass, progress=lambda done, total: seen.append((done, total)))
    assert len(seen) > 1
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert seen[-1] == (len(data), len(data))


@pytest.mark.asyncio
async def test_progress_has_no_total_when_the_server_announces_none(
    hass, aioclient_mock, storage, known
):
    data = known(_dataset())
    aioclient_mock.get(_URL, content=data)
    seen: list[tuple[int, int | None]] = []
    await _download(hass, progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (len(data), None)


@pytest.mark.asyncio
async def test_a_downloaded_dataset_is_what_the_reader_then_loads(
    hass, aioclient_mock, storage, known
):
    aioclient_mock.get(_URL, content=known(_dataset()))
    await _download(hass)
    assert len(await async_get_zone_data(hass).async_acquire("entry-1")) == 3


# ---------------------------------------------------------------------------
# Failures: each says what it was, and the fixed path is left alone
# ---------------------------------------------------------------------------


@pytest.fixture
def working(storage, known):
    """A good dataset already in place, to prove a failure does not disturb it."""
    data = known(_dataset(_ZONES[:2]), "2025-09-10")
    storage.parent.mkdir(parents=True)
    storage.write_bytes(data)
    return data


async def _fails(hass, storage, working: bytes, reason: str) -> ZoneDataFetchError:
    with pytest.raises(ZoneDataFetchError) as raised:
        await _download(hass)
    assert raised.value.reason == reason
    assert storage.read_bytes() == working
    assert _leftovers(storage) == []
    return raised.value


@pytest.mark.asyncio
async def test_an_http_error_is_a_download_failure(
    hass, aioclient_mock, storage, working
):
    aioclient_mock.get(_URL, status=404)
    await _fails(hass, storage, working, REASON_DOWNLOAD)


@pytest.mark.asyncio
async def test_a_network_error_is_a_download_failure(
    hass, aioclient_mock, storage, working
):
    aioclient_mock.get(_URL, exc=aiohttp.ClientConnectionError("refused"))
    await _fails(hass, storage, working, REASON_DOWNLOAD)


@pytest.mark.asyncio
async def test_a_timeout_is_a_download_failure(hass, aioclient_mock, storage, working):
    aioclient_mock.get(_URL, exc=TimeoutError())
    await _fails(hass, storage, working, REASON_DOWNLOAD)


@pytest.mark.asyncio
async def test_a_download_failure_does_not_repeat_the_address(
    hass, aioclient_mock, storage, working
):
    """The caller supplied the URL; an error message has no need to carry it."""
    aioclient_mock.get(_URL, status=500)
    error = await _fails(hass, storage, working, REASON_DOWNLOAD)
    assert "example.invalid" not in str(error)

    # Nor when the HTTP client's own error quotes it, as several of them do.
    aioclient_mock.clear_requests()
    aioclient_mock.get(_URL, exc=aiohttp.ClientConnectionError(f"cannot reach {_URL}"))
    error = await _fails(hass, storage, working, REASON_DOWNLOAD)
    assert "example.invalid" not in str(error)
    assert "ClientConnectionError" in str(error)


@pytest.mark.asyncio
async def test_a_response_announced_as_too_large_is_refused_unread(
    hass, aioclient_mock, storage, working
):
    aioclient_mock.get(_URL, content=b"x" * 10, headers={"Content-Length": "999999999"})
    await _fails(hass, storage, working, REASON_TOO_LARGE)


@pytest.mark.asyncio
async def test_a_response_that_grows_past_the_ceiling_is_abandoned(
    hass, aioclient_mock, storage, working
):
    """The ceiling holds whatever the headers claimed, or did not."""
    aioclient_mock.get(_URL, content=b"x" * 5000)
    with (
        patch.object(nws_zone_fetch, "MAX_ZONE_DATA_BYTES", 1000),
        patch.object(nws_zone_fetch, "_CHUNK", 256),
    ):
        await _fails(hass, storage, working, REASON_TOO_LARGE)


@pytest.mark.asyncio
async def test_a_sound_file_that_was_never_published_is_not_installed(
    hass, aioclient_mock, storage, working
):
    aioclient_mock.get(_URL, content=_dataset(_ZONES[:1]))
    await _fails(hass, storage, working, REASON_UNRECOGNISED)


@pytest.mark.asyncio
async def test_an_error_page_is_not_installed(hass, aioclient_mock, storage, working):
    aioclient_mock.get(_URL, content=b"<html>Not here</html>")
    await _fails(hass, storage, working, REASON_UNRECOGNISED)


@pytest.mark.asyncio
async def test_a_known_file_that_does_not_read_back_whole_is_not_installed(
    hass, aioclient_mock, storage, working, known
):
    broken = known(_dataset()[:-20])
    aioclient_mock.get(_URL, content=broken)
    with pytest.raises(ZoneDataFetchError) as raised:
        await _download(hass, sha256=_sha256(broken))
    assert raised.value.reason == REASON_DAMAGED
    assert storage.read_bytes() == working
    assert _leftovers(storage) == []


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_old_file_and_no_partial_one(
    hass, aioclient_mock, storage, working, known
):
    aioclient_mock.get(_URL, content=known(_dataset()))
    with patch.object(nws_zone_fetch.os, "replace", side_effect=OSError("disk full")):
        await _fails(hass, storage, working, REASON_WRITE)


@pytest.mark.asyncio
async def test_nothing_is_written_before_verification(hass, aioclient_mock, storage):
    """With no file in place, a rejected download creates none."""
    aioclient_mock.get(_URL, content=_dataset(_ZONES[:1]))
    with pytest.raises(ZoneDataFetchError):
        await _download(hass)
    assert not storage.exists()
    assert _leftovers(storage) == []


# ---------------------------------------------------------------------------
# Entries that are running, and entries that are not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_entries_get_the_new_dataset(
    hass, aioclient_mock, storage, working, known
):
    zone_data = async_get_zone_data(hass)
    old = await zone_data.async_acquire("entry-1")
    assert len(old) == 2

    aioclient_mock.get(_URL, content=_WANTED)
    await _download(hass)

    new = await zone_data.async_acquire("entry-1")
    assert new is not old
    assert len(new) == 3
    assert zone_data.release_date == "2026-04-16"


@pytest.mark.asyncio
async def test_a_failed_download_leaves_running_entries_on_the_old_dataset(
    hass, aioclient_mock, storage, working
):
    zone_data = async_get_zone_data(hass)
    old = await zone_data.async_acquire("entry-1")
    aioclient_mock.get(_URL, content=b"not a dataset")
    with pytest.raises(ZoneDataFetchError):
        await _download(hass)
    assert await zone_data.async_acquire("entry-1") is old


@pytest.mark.asyncio
async def test_a_download_with_no_entry_using_it_loads_nothing(
    hass, aioclient_mock, storage, known
):
    """Fetching the file is not using it: no loader object appears, nothing is held."""
    aioclient_mock.get(_URL, content=known(_dataset()))
    await _download(hass)
    assert "nws_zone_data" not in hass.data[DOMAIN]
    assert not await async_reload_zone_data(hass)


@pytest.mark.asyncio
async def test_two_downloads_at_once_do_not_collide(
    hass, aioclient_mock, storage, known
):
    data = known(_dataset())
    aioclient_mock.get(_URL, content=data)
    first, second = await asyncio.gather(_download(hass), _download(hass))
    assert first == second == _sha256(data)
    assert storage.read_bytes() == data
    assert _leftovers(storage) == []


# ---------------------------------------------------------------------------
# A file supplied by hand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hand_supplied_file_passes_the_same_check(hass, storage, known):
    data = known(_dataset())
    storage.parent.mkdir(parents=True)
    storage.write_bytes(data)
    assert await async_adopt_zone_data(hass) == _sha256(data)


@pytest.mark.asyncio
async def test_adopting_swaps_the_file_in_for_running_entries(
    hass, storage, working, known
):
    zone_data = async_get_zone_data(hass)
    old = await zone_data.async_acquire("entry-1")
    storage.write_bytes(known(_dataset(), "2026-04-16"))
    await async_adopt_zone_data(hass)
    assert await zone_data.async_acquire("entry-1") is not old
    assert zone_data.release_date == "2026-04-16"


@pytest.mark.asyncio
async def test_adopting_with_nothing_there_says_missing(hass, storage):
    with pytest.raises(ZoneDataFetchError) as raised:
        await async_adopt_zone_data(hass)
    assert raised.value.reason == REASON_MISSING


@pytest.mark.asyncio
async def test_a_hand_supplied_file_that_was_never_published_is_refused_and_left(
    hass, storage
):
    """Left in place for the person to replace; the error says why."""
    data = _dataset(_ZONES[:1])
    storage.parent.mkdir(parents=True)
    storage.write_bytes(data)
    with pytest.raises(ZoneDataFetchError) as raised:
        await async_adopt_zone_data(hass)
    assert raised.value.reason == REASON_UNRECOGNISED
    assert storage.read_bytes() == data


# ---------------------------------------------------------------------------
# A hostile server
#
# Assume whatever answers the request wants to get a wrong file installed,
# send the request somewhere it should not go, or exhaust the machine.
# ---------------------------------------------------------------------------

_ASSETS = "release-assets.example.invalid"
_ASSET_URL = f"https://{_ASSETS}/abc/nws_zones.jsonl.gz"


def _requested(aioclient_mock) -> list[str]:
    return [str(url) for _method, url, _data, _headers in aioclient_mock.mock_calls]


@pytest.mark.asyncio
async def test_an_older_published_dataset_is_not_accepted_in_place_of_the_one_asked_for(
    hass, aioclient_mock, storage, working
):
    """A rollback: real, published, correctly hashed, and stale. Refused."""
    aioclient_mock.get(_URL, content=working)
    await _fails(hass, storage, working, REASON_UNEXPECTED)


@pytest.mark.asyncio
async def test_asking_for_a_dataset_that_was_never_published_makes_no_request(
    hass, aioclient_mock, storage
):
    with pytest.raises(ZoneDataFetchError) as raised:
        await _download(hass, sha256="0" * 64)
    assert raised.value.reason == REASON_UNRECOGNISED
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    "address",
    [
        "http://example.invalid/nws_zones.jsonl.gz",
        "ftp://example.invalid/nws_zones.jsonl.gz",
        "file:///etc/passwd",
        "https://user:secret@example.invalid/nws_zones.jsonl.gz",
        "https://example.invalid:8443/nws_zones.jsonl.gz",
        "https:///nws_zones.jsonl.gz",
        "https://[::1/nws_zones.jsonl.gz",  # not parseable at all
        "not an address",
    ],
)
@pytest.mark.asyncio
async def test_an_address_that_is_not_plain_https_is_never_requested(
    hass, aioclient_mock, storage, address
):
    with pytest.raises(ZoneDataFetchError) as raised:
        await _download(hass, url=address)
    assert raised.value.reason == REASON_BLOCKED
    assert aioclient_mock.call_count == 0


@pytest.mark.asyncio
async def test_a_redirect_to_an_allowed_host_is_followed(hass, aioclient_mock, storage):
    """Release files are served from a second host; the caller names it."""
    aioclient_mock.get(_URL, status=302, headers={"Location": _ASSET_URL})
    aioclient_mock.get(_ASSET_URL, content=_WANTED)
    assert await _download(hass, allowed_hosts=[_ASSETS]) == _sha256(_WANTED)
    assert _requested(aioclient_mock) == [_URL, _ASSET_URL]
    assert storage.read_bytes() == _WANTED


@pytest.mark.parametrize(
    "location",
    [
        "https://elsewhere.invalid/nws_zones.jsonl.gz",  # a host nobody allowed
        "http://example.invalid/nws_zones.jsonl.gz",  # the same host, without TLS
        "http://192.168.0.1/admin/reboot",  # a device on the local network
        "http://localhost:8123/api/",  # Home Assistant itself
        "file:///etc/passwd",
        "//elsewhere.invalid/nws_zones.jsonl.gz",  # scheme-relative
        "https://[::1/nws_zones.jsonl.gz",  # not parseable at all
    ],
)
@pytest.mark.asyncio
async def test_a_redirect_anywhere_else_is_not_followed(
    hass, aioclient_mock, storage, working, location
):
    aioclient_mock.get(_URL, status=302, headers={"Location": location})
    await _fails(hass, storage, working, REASON_BLOCKED)
    assert _requested(aioclient_mock) == [_URL]  # the second request was never made


@pytest.mark.asyncio
async def test_a_redirect_loop_is_cut_off(hass, aioclient_mock, storage, working):
    aioclient_mock.get(_URL, status=302, headers={"Location": _URL})
    await _fails(hass, storage, working, REASON_BLOCKED)
    assert aioclient_mock.call_count == nws_zone_fetch.MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_a_redirect_that_names_no_address_is_a_download_failure(
    hass, aioclient_mock, storage, working
):
    aioclient_mock.get(_URL, status=302)
    await _fails(hass, storage, working, REASON_DOWNLOAD)


@pytest.mark.parametrize("status", [201, 204, 206, 304, 401, 403, 500])
@pytest.mark.asyncio
async def test_only_a_plain_200_is_accepted(
    hass, aioclient_mock, storage, working, status
):
    aioclient_mock.get(_URL, status=status, content=_WANTED)
    await _fails(hass, storage, working, REASON_DOWNLOAD)


@pytest.mark.asyncio
async def test_the_request_asks_for_no_transfer_compression(
    hass, aioclient_mock, storage
):
    aioclient_mock.get(_URL, content=_WANTED)
    await _download(hass)
    _method, _url, _data, headers = aioclient_mock.mock_calls[0]
    assert headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "encoding", ["gzip", "deflate", "br", "zstd", "GZIP", "gzip, br"]
)
@pytest.mark.asyncio
async def test_a_transfer_compressed_response_is_refused_unread(
    hass, aioclient_mock, storage, working, encoding
):
    """Nothing an attacker sends is handed to a decompressor before its hash is known."""
    aioclient_mock.get(_URL, content=_WANTED, headers={"Content-Encoding": encoding})
    with patch.object(
        nws_zone_fetch, "verify_zone_data", side_effect=AssertionError("body was used")
    ):
        await _fails(hass, storage, working, REASON_BLOCKED)


@pytest.mark.parametrize(
    "announced", [1, len(_WANTED) - 1, len(_WANTED) + 1, 10 * len(_WANTED)]
)
@pytest.mark.asyncio
async def test_a_body_that_is_not_the_announced_size_is_refused(
    hass, aioclient_mock, storage, working, announced
):
    """Even when the bytes that did arrive are the right file."""
    aioclient_mock.get(
        _URL, content=_WANTED, headers={"Content-Length": str(announced)}
    )
    await _fails(hass, storage, working, REASON_DOWNLOAD)


@pytest.mark.parametrize("announced", ["-1", "many", "", "1e3"])
@pytest.mark.asyncio
async def test_a_nonsense_announced_size_is_ignored_not_trusted(
    hass, aioclient_mock, storage, announced
):
    aioclient_mock.get(_URL, content=_WANTED, headers={"Content-Length": announced})
    assert await _download(hass) == _sha256(_WANTED)


@pytest.mark.asyncio
async def test_the_ceiling_applies_at_every_hop(hass, aioclient_mock, storage, working):
    aioclient_mock.get(_URL, status=302, headers={"Location": _ASSET_URL})
    aioclient_mock.get(_ASSET_URL, content=b"x" * 5000)
    with (
        patch.object(nws_zone_fetch, "MAX_ZONE_DATA_BYTES", 1000),
        patch.object(nws_zone_fetch, "_CHUNK", 256),
        pytest.raises(ZoneDataFetchError) as raised,
    ):
        await _download(hass, allowed_hosts=[_ASSETS])
    assert raised.value.reason == REASON_TOO_LARGE
    assert storage.read_bytes() == working


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
@pytest.mark.asyncio
async def test_a_link_planted_where_the_partial_file_goes_is_not_written_through(
    hass, aioclient_mock, storage, working, tmp_path
):
    """Otherwise the download would overwrite whatever the link points at.
    The link itself is removed and the .part file made new beside it."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    storage.with_name(storage.name + ".part").symlink_to(victim)
    aioclient_mock.get(_URL, content=_WANTED)

    assert await _download(hass) == _sha256(_WANTED)

    assert victim.read_bytes() == b"precious"
    assert storage.read_bytes() == _WANTED
    assert not storage.is_symlink()
    assert _leftovers(storage) == []


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
@pytest.mark.asyncio
async def test_a_link_planted_again_at_the_last_moment_is_a_failed_write(
    hass, aioclient_mock, storage, working, tmp_path
):
    """Between the removal and the creation something puts a link back. The
    exclusive creation fails, and nothing is written through the link."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    part = storage.with_name(storage.name + ".part")
    real_open = os.open

    def planted(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(".part") and not part.is_symlink():
            part.symlink_to(victim)
        return real_open(path, flags, mode, **kwargs)

    aioclient_mock.get(_URL, content=_WANTED)
    with (
        patch.object(nws_zone_fetch.os, "open", planted),
        pytest.raises(ZoneDataFetchError) as raised,
    ):
        await _download(hass)

    assert raised.value.reason == REASON_WRITE
    assert victim.read_bytes() == b"precious"
    assert storage.read_bytes() == working


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.asyncio
async def test_the_installed_file_is_private(hass, aioclient_mock, storage):
    aioclient_mock.get(_URL, content=_WANTED)
    await _download(hass)
    assert stat.S_IMODE(storage.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Whatever is already where the .part file goes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
async def test_a_pipe_left_where_the_part_file_goes_does_not_hang_the_install(
    hass, aioclient_mock, storage, working
):
    """Opening a named pipe for writing waits for a reader that never comes.
    The .part file is made new each time, so what was there is not opened."""
    part = storage.with_name(storage.name + ".part")
    os.mkfifo(part)
    aioclient_mock.get(_URL, content=_WANTED)

    async with asyncio.timeout(10):
        assert await _download(hass) == _sha256(_WANTED)

    assert storage.read_bytes() == _WANTED
    assert _leftovers(storage) == []


@pytest.mark.asyncio
async def test_a_stale_part_file_is_replaced_not_appended_to(
    hass, aioclient_mock, storage, working
):
    part = storage.with_name(storage.name + ".part")
    part.write_bytes(b"left by an install that was cut short" * 1000)
    aioclient_mock.get(_URL, content=_WANTED)

    assert await _download(hass) == _sha256(_WANTED)

    assert storage.read_bytes() == _WANTED
    assert _leftovers(storage) == []


@pytest.mark.asyncio
async def test_a_directory_where_the_part_file_goes_is_a_failed_write(
    hass, aioclient_mock, storage, working
):
    """It is not removed, and the dataset in place is not touched."""
    part = storage.with_name(storage.name + ".part")
    part.mkdir()
    (part / "kept").write_text("not ours")
    aioclient_mock.get(_URL, content=_WANTED)

    with pytest.raises(ZoneDataFetchError) as raised:
        await _download(hass)

    assert raised.value.reason == REASON_WRITE
    assert storage.read_bytes() == working
    assert (part / "kept").read_text() == "not ours"


@pytest.mark.asyncio
async def test_the_part_file_is_private_and_made_new(hass, aioclient_mock, storage):
    """Created by this install and nobody else: exclusive, no link followed,
    readable by its owner only."""
    seen: list[tuple[int, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(".part"):
            seen.append((flags, mode))
        return real_open(path, flags, mode, **kwargs)

    aioclient_mock.get(_URL, content=_WANTED)
    with patch.object(nws_zone_fetch.os, "open", spy):
        await _download(hass)

    ((flags, mode),) = seen
    assert flags & os.O_EXCL and flags & os.O_CREAT
    assert flags & getattr(os, "O_NOFOLLOW", 0) == getattr(os, "O_NOFOLLOW", 0)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# A file that cannot be read back after it was put in place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reload_that_fails_is_reported_as_a_fetch_failure(
    hass, aioclient_mock, storage
):
    """Every failure leaves as ZoneDataFetchError with a reason, also one
    from handing the new file to the entries that are running."""
    aioclient_mock.get(_URL, content=_WANTED)
    gone = ZoneDataUnavailableError(REASON_DAMAGED, "changed after it was written")
    with (
        patch.object(nws_zone_fetch, "async_reload_zone_data", side_effect=gone),
        pytest.raises(ZoneDataFetchError) as raised,
    ):
        await _download(hass)
    assert raised.value.reason == REASON_DAMAGED

    with (
        patch.object(nws_zone_fetch, "async_reload_zone_data", side_effect=gone),
        pytest.raises(ZoneDataFetchError) as raised,
    ):
        await async_adopt_zone_data(hass)
    assert raised.value.reason == REASON_DAMAGED


# ---------------------------------------------------------------------------
# A host name that cannot be read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["https://xn--/zones.gz", "https://github.com.xn--/zones.gz"],
    ids=["nothing after the prefix", "an empty last label"],
)
@pytest.mark.asyncio
async def test_a_host_name_that_cannot_be_decoded_is_blocked_at_the_start(
    hass, aioclient_mock, storage, working, address
):
    """The address parses; its host raises only when it is read. Found by
    fuzzing: the error left as it was, not as a fetch failure."""
    with pytest.raises(ZoneDataFetchError) as raised:
        await _download(hass, url=address)
    assert raised.value.reason == REASON_BLOCKED
    assert _requested(aioclient_mock) == []
    assert storage.read_bytes() == working


@pytest.mark.asyncio
async def test_a_redirect_to_a_host_name_that_cannot_be_decoded_is_blocked(
    hass, aioclient_mock, storage, working
):
    aioclient_mock.get(
        _URL, status=302, headers={"Location": "https://xn--.example/zones.gz"}
    )
    with pytest.raises(ZoneDataFetchError) as raised:
        await _download(hass)
    assert raised.value.reason == REASON_BLOCKED
    assert _requested(aioclient_mock) == [_URL]
    assert storage.read_bytes() == working


# ---------------------------------------------------------------------------
# Taking the dataset away again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_removing_deletes_the_dataset_and_a_part_file_left_behind(
    hass, storage, working
):
    part = storage.with_name(storage.name + ".part")
    part.write_bytes(b"an install that was cut short")
    beside = storage.with_name("cap_alerts.something_else")
    beside.write_bytes(b"not the dataset")

    assert await async_remove_zone_data(hass) is True

    assert not storage.exists() and not part.exists()
    assert beside.read_bytes() == b"not the dataset"


@pytest.mark.asyncio
async def test_removing_with_nothing_there_reports_that_nothing_went(hass, storage):
    assert await async_remove_zone_data(hass) is False
    # Only a .part file: it goes, and no dataset was removed.
    storage.parent.mkdir(parents=True)
    part = storage.with_name(storage.name + ".part")
    part.write_bytes(b"x")
    assert await async_remove_zone_data(hass) is False
    assert not part.exists()


@pytest.mark.asyncio
async def test_removing_a_link_removes_the_link_and_not_what_it_points_to(
    hass, storage, tmp_path
):
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    storage.parent.mkdir(parents=True)
    storage.symlink_to(victim)

    assert await async_remove_zone_data(hass) is True

    assert not storage.is_symlink()
    assert victim.read_bytes() == b"precious"


@pytest.mark.asyncio
async def test_what_cannot_be_removed_is_logged_and_does_not_raise(
    hass, storage, caplog
):
    """A directory where the file should be. The caller is the removal of
    an entry, which must not fail over it."""
    storage.mkdir(parents=True)
    (storage / "kept").write_text("not ours")

    assert await async_remove_zone_data(hass) is False

    assert (storage / "kept").read_text() == "not ours"
    assert "could not be removed" in caplog.text
    assert str(storage.parent) not in caplog.text  # the name, not the path


@pytest.mark.asyncio
async def test_removing_waits_for_an_install_in_progress(hass, aioclient_mock, storage):
    """Under the same lock: a removal asked for while an install is writing
    happens after it, so it removes a whole file and leaves no half of one."""
    aioclient_mock.get(_URL, content=_WANTED)
    order: list[str] = []
    real_write = nws_zone_fetch._write_atomic
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_write(path, stored):
        loop.call_soon_threadsafe(started.set)
        time.sleep(0.2)
        real_write(path, stored)
        order.append("written")

    real_remove = nws_zone_fetch._remove

    def remove(path):
        order.append("removed")
        return real_remove(path)

    with (
        patch.object(nws_zone_fetch, "_write_atomic", slow_write),
        patch.object(nws_zone_fetch, "_remove", remove),
    ):
        install = asyncio.create_task(_download(hass))
        await started.wait()
        assert await async_remove_zone_data(hass) is True
        await install

    assert order == ["written", "removed"]
    assert not storage.exists()
    assert _leftovers(storage) == []
