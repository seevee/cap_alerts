"""Unit tests for the NAAD streaming client.

Exercises the transport in isolation — frame reassembly, heartbeat
classification, the silence watchdog, reconnect/backoff, and clean teardown —
by driving a scripted ``asyncio.StreamReader`` through an injected ``connect``
callable, with no socket and no Home Assistant runtime.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
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

    def close(self) -> None:
        self.closed = True


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
    monkeypatch.setattr(_naad_mod, "_MAX_BUFFER_BYTES", 32)
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


def test_stop_closes_writer():
    client = _make_client()
    writer = _FakeWriter()
    client._writer = writer
    client.stop()
    assert writer.closed is True
