"""Unit tests for the NAAD streaming client.

Exercises the transport in isolation — frame reassembly, heartbeat
classification, the silence watchdog, reconnect/backoff, and clean teardown —
by driving a scripted ``asyncio.StreamReader`` through an injected ``connect``
callable, with no socket and no Home Assistant runtime.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import ssl
import sys
import threading
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_ROOT / "custom_components" / "cap_alerts"


def _load(name: str) -> types.ModuleType:
    full = f"cap_alerts.{name}"
    if full in sys.modules:
        return sys.modules[full]
    pkg = sys.modules.get("cap_alerts")
    if pkg is None:
        pkg = types.ModuleType("cap_alerts")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["cap_alerts"] = pkg
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_provider(name: str) -> types.ModuleType:
    full = f"cap_alerts.providers.{name}"
    if full in sys.modules:
        return sys.modules[full]
    pkg_key = "cap_alerts.providers"
    if pkg_key not in sys.modules:
        providers_pkg = types.ModuleType(pkg_key)
        providers_pkg.__path__ = [str(_PKG_DIR / "providers")]
        sys.modules[pkg_key] = providers_pkg
    spec = importlib.util.spec_from_file_location(
        full, _PKG_DIR / "providers" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
_naad_mod = _load_provider("naad_stream")
NAADStreamClient = _naad_mod.NAADStreamClient


# ---------------------------------------------------------------------------
# Sample wire documents
# ---------------------------------------------------------------------------

_ALERT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
    "<identifier>urn:oid:alert-1</identifier>"
    "<sender>cap-pac@canada.ca</sender>"
    "<status>Actual</status></alert>"
)
_HEARTBEAT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
    "<identifier>urn:oid:hb-1</identifier>"
    "<sender>NAADS-Heartbeat@naad-adna.pelmorex.com</sender>"
    "<status>System</status></alert>"
)


def _make_client(**overrides):
    """Construct a client with no-op callbacks; overrides replace any of them."""

    async def _noop(*_args) -> None:
        return None

    kwargs = {
        "on_alert_doc": _noop,
        "on_heartbeat": _noop,
        "on_backfill_needed": _noop,
        "connect": _noop,  # unused unless run() is called
        "backoff_min_s": 0,
        "backoff_max_s": 0,
    }
    kwargs.update(overrides)
    return NAADStreamClient("host", 8443, **kwargs)


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.awaited_closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.awaited_closed = True


def _reader(*chunks: str, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk.encode("utf-8"))
    if eof:
        reader.feed_eof()
    return reader


# ---------------------------------------------------------------------------
# Frame reassembly
# ---------------------------------------------------------------------------


def test_extract_docs_multiple_frames_in_one_buffer():
    client = _make_client()
    buffer, docs = client._extract_docs("<alert>1</alert><alert>2</alert>")
    assert docs == ["<alert>1</alert>", "<alert>2</alert>"]
    assert buffer == ""


def test_extract_docs_skips_xml_declaration_between_frames():
    client = _make_client()
    _buffer, docs = client._extract_docs(
        '<alert>1</alert><?xml version="1.0"?><alert>2</alert>'
    )
    assert docs == ["<alert>1</alert>", "<alert>2</alert>"]


def test_extract_docs_holds_incomplete_frame():
    client = _make_client()
    buffer, docs = client._extract_docs("<alert>partial")
    assert docs == []
    assert buffer == "<alert>partial"


def test_extract_docs_reassembles_split_frame():
    client = _make_client()
    buffer, docs = client._extract_docs('<?xml version="1.0"?><alert>a</alert><al')
    assert docs == ["<alert>a</alert>"]
    buffer, docs = client._extract_docs(buffer + "ert>b</alert>")
    assert docs == ["<alert>b</alert>"]


def test_extract_docs_discards_over_long_unterminated_buffer(monkeypatch):
    monkeypatch.setattr(_naad_mod, "_MAX_BUFFER_CHARS", 32)
    client = _make_client()
    buffer, docs = client._extract_docs("<alert>" + "x" * 100)
    assert docs == []
    assert buffer == ""


# ---------------------------------------------------------------------------
# Heartbeat classification
# ---------------------------------------------------------------------------


def test_is_heartbeat_detects_naads_heartbeat_sender():
    client = _make_client()
    assert client._is_heartbeat(_HEARTBEAT) is True


def test_is_heartbeat_false_for_regular_alert():
    client = _make_client()
    assert client._is_heartbeat(_ALERT) is False


def test_is_heartbeat_detects_system_status_without_sender():
    client = _make_client()
    assert client._is_heartbeat("<alert><status>System</status></alert>") is True


# ---------------------------------------------------------------------------
# Read loop: dispatch + watchdog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_loop_dispatches_alerts_and_heartbeats():
    alerts: list[str] = []
    heartbeats: list[int] = []

    async def on_alert(doc: str) -> None:
        alerts.append(doc)

    async def on_hb() -> None:
        heartbeats.append(1)

    client = _make_client(on_alert_doc=on_alert, on_heartbeat=on_hb)
    await client._read_loop(_reader(_ALERT, _HEARTBEAT))

    assert len(alerts) == 1
    assert "urn:oid:alert-1" in alerts[0]
    assert len(heartbeats) == 1


@pytest.mark.asyncio
async def test_read_loop_decodes_multibyte_char_split_across_reads():
    """A UTF-8 character straddling two reads must survive intact (bilingual feed)."""
    docs: list[str] = []

    async def on_alert(doc: str) -> None:
        docs.append(doc)

    text = "<alert><headline>Avertissement de grêle</headline></alert>"
    raw = text.encode("utf-8")
    cut = raw.index("ê".encode()) + 1  # split inside the 2-byte sequence

    reader = asyncio.StreamReader()
    reader.feed_data(raw[:cut])
    reader.feed_data(raw[cut:])
    reader.feed_eof()

    client = _make_client(on_alert_doc=on_alert)
    await client._read_loop(reader)

    assert docs == [text]


@pytest.mark.asyncio
async def test_read_loop_reports_whether_data_arrived():
    """The return value is what run()'s backoff policy keys off."""
    client = _make_client()
    assert await client._read_loop(_reader(_ALERT)) is True
    assert await client._read_loop(_reader()) is False


@pytest.mark.asyncio
async def test_read_loop_returns_on_heartbeat_silence():
    """No data within the heartbeat timeout returns (to trigger a reconnect)."""
    client = _make_client(heartbeat_timeout_s=0.02)
    reader = asyncio.StreamReader()  # never fed, never eof

    # Should return promptly via the read timeout rather than hang.
    await asyncio.wait_for(client._read_loop(reader), timeout=1.0)


# ---------------------------------------------------------------------------
# run(): reconnect, backfill-on-reconnect, teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backfills_on_reconnect_then_stops():
    connects: list[int] = []
    backfills: list[int] = []
    holder: dict[str, NAADStreamClient] = {}

    async def connect():
        connects.append(1)
        return _reader(eof=True), _FakeWriter()  # immediate EOF → reconnect

    async def on_backfill_needed():
        backfills.append(1)
        holder["client"].stop()  # end the loop after the first reconnect backfill

    client = _make_client(connect=connect, on_backfill_needed=on_backfill_needed)
    holder["client"] = client
    await asyncio.wait_for(client.run(), timeout=1.0)

    # First connect: no backfill (coordinator already seeded). Reconnect: backfill.
    assert len(connects) == 2
    assert len(backfills) == 1


@pytest.mark.asyncio
async def test_run_retries_connect_failures_with_bounded_backoff():
    attempts: list[int] = []
    holder: dict[str, NAADStreamClient] = {}

    async def connect():
        attempts.append(1)
        if len(attempts) >= 3:
            holder["client"].stop()
            return _reader(eof=True), _FakeWriter()
        raise ConnectionRefusedError("nope")

    client = _make_client(connect=connect, backoff_min_s=0, backoff_max_s=0)
    holder["client"] = client
    await asyncio.wait_for(client.run(), timeout=1.0)

    # Two failed connects retried, third succeeds and stops the loop.
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_run_warns_once_per_connect_failure_streak_and_logs_recovery(caplog):
    """A dead socket must be visible at default log level, but must not spam.

    Without this, an endpoint that never accepts produces nothing above ``debug``
    and the integration silently degrades to the 30-minute resync — the user sees
    late alerts and an empty log.
    """
    attempts: list[int] = []
    holder: dict[str, NAADStreamClient] = {}

    async def connect():
        attempts.append(1)
        if len(attempts) >= 4:
            holder["client"].stop()
            return _reader(eof=True), _FakeWriter()
        raise ConnectionRefusedError("nope")

    client = _make_client(connect=connect, backoff_min_s=0, backoff_max_s=0)
    holder["client"] = client
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(client.run(), timeout=1.0)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "three failed connects should warn once, not thrice"
    assert "cannot connect" in warnings[0].getMessage()

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "after 3 failed attempt(s)" in infos[0].getMessage()


@pytest.mark.asyncio
async def test_run_does_not_warn_when_the_first_connect_succeeds(caplog):
    """The healthy path stays quiet at default level."""

    async def connect():
        client.stop()
        return _reader(eof=True), _FakeWriter()

    client = _make_client(connect=connect)
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(client.run(), timeout=1.0)

    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


@pytest.mark.asyncio
async def test_run_grows_backoff_when_connection_delivers_nothing():
    """Accept-then-close must back off, not hot-loop.

    Every reconnect costs a full GeoRSS backfill, so a server that accepts the
    connection and immediately drops it must not be retried in a tight loop.
    """
    slept: list[float] = []

    async def connect():
        return _reader(eof=True), _FakeWriter()  # accepts, delivers nothing

    client = _make_client(connect=connect, backoff_min_s=1, backoff_max_s=8)

    async def _record(backoff: float) -> None:
        slept.append(backoff)
        if len(slept) >= 4:
            client.stop()

    client._sleep_backoff = _record
    await asyncio.wait_for(client.run(), timeout=1.0)

    assert slept == [1, 2, 4, 8]  # doubling, clamped at the ceiling


@pytest.mark.asyncio
async def test_run_resets_backoff_after_a_productive_connection():
    """A connection that delivered data starts the next backoff back at the floor."""
    slept: list[float] = []
    connects: list[int] = []

    async def connect():
        connects.append(1)
        # Third connection delivers a doc; the rest close silently.
        if len(connects) == 3:
            return _reader(_ALERT), _FakeWriter()
        return _reader(eof=True), _FakeWriter()

    client = _make_client(connect=connect, backoff_min_s=1, backoff_max_s=8)

    async def _record(backoff: float) -> None:
        slept.append(backoff)
        if len(slept) >= 4:
            client.stop()

    client._sleep_backoff = _record
    await asyncio.wait_for(client.run(), timeout=1.0)

    assert slept == [1, 2, 1, 2]  # grew, reset after the productive connection


@pytest.mark.asyncio
async def test_run_awaits_tls_shutdown_on_disconnect():
    """close() alone leaves the TLS shutdown pending; run() waits for it."""
    writers: list[_FakeWriter] = []

    async def connect():
        writer = _FakeWriter()
        writers.append(writer)
        if len(writers) >= 2:
            client.stop()
        return _reader(eof=True), writer

    client = _make_client(connect=connect)
    await asyncio.wait_for(client.run(), timeout=1.0)

    assert writers[0].closed is True
    assert writers[0].awaited_closed is True


@pytest.mark.asyncio
async def test_default_connect_uses_an_injected_ssl_context(monkeypatch):
    """An injected context is passed straight through — none is built."""
    sentinel = ssl.create_default_context()
    opened: list[tuple] = []

    async def _fake_open_connection(host, port, ssl=None):
        opened.append((host, port, ssl))
        return object(), object()

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("built an SSL context despite one being injected")

    monkeypatch.setattr(asyncio, "open_connection", _fake_open_connection)
    monkeypatch.setattr(ssl, "create_default_context", _forbidden)

    client = _make_client(connect=None, ssl_context=sentinel)
    await client._default_connect()

    assert opened == [("host", 8443, sentinel)]


@pytest.mark.asyncio
async def test_default_connect_builds_ssl_context_off_the_event_loop(monkeypatch):
    """The fallback context is built in an executor and reused across reconnects.

    Loading the CA bundle is disk I/O; doing it inline trips Home Assistant's
    blocking-call detector.
    """
    loop_thread = threading.get_ident()
    build_threads: list[int] = []
    built = ssl.create_default_context()

    def _record() -> ssl.SSLContext:
        build_threads.append(threading.get_ident())
        return built

    async def _fake_open_connection(host, port, ssl=None):
        return object(), object()

    monkeypatch.setattr(asyncio, "open_connection", _fake_open_connection)
    monkeypatch.setattr(ssl, "create_default_context", _record)

    client = _make_client(connect=None, ssl_context=None)
    await client._default_connect()
    await client._default_connect()

    assert len(build_threads) == 1, "context rebuilt on reconnect"
    assert build_threads[0] != loop_thread, "context built on the event loop"
    assert client._ssl_context is built


def test_stop_closes_writer():
    client = _make_client()
    writer = _FakeWriter()
    client._writer = writer
    client.stop()
    assert writer.closed is True
