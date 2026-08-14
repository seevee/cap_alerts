#!/usr/bin/env python3
"""Does the NAAD streaming socket carry what the alertready GeoRSS index drops?

Companion to ``naad_feed_diff.py``, which established that rss.alertready.ca
serves a mean 84.6% of the ids rss.naad-adna.pelmorex.com carries (1422 samples,
2026-07-30 onward, never 100%). The dropped alerts are retrievable from
cap.alertready.ca by identifier, so the defect is the GeoRSS index generator
rather than ingest — which leaves one question unanswered, and it is the one
that decides whether the pelmorex sunset (~Sept 2026) costs users alerts:

    does streaming.alertready.ca:8443 deliver the alerts rss.alertready.ca
    omits, or does the socket share the same backing?

If the socket is complete, the sunset is survivable and the GeoRSS index demotes
to backfill. If it shares the gap, every ECCC user loses Extreme alerts when
pelmorex retires, because the integration has defaulted to this socket since
0.2.0.

Method: hold the TLS stream open, record every CAP identifier it delivers, and
every ``--interval`` seconds fetch both GeoRSS hosts and bucket every alert
either host carries:

    both                     on alertready's index and on the socket
    rss_gap_stream_has       NOT on the index, on the socket   <- index-only bug
    rss_gap_stream_missing   NOT on the index, NOT on the socket <- shared gap
    rss_has_stream_missing   on the index, not on the socket

Only alerts that could honestly have been streamed are bucketed. A socket
carries what is issued while you are connected, so anything published before the
session started (plus ``--warmup``) is excluded, as is anything published while
the socket was down. Both exclusions are counted in every record.

Identity is the CAP ``<identifier>`` against the Atom id's ``feed.atom/`` suffix.
Every record reports how many streamed ids matched a GeoRSS id at all, so a
wrong identity assumption shows up as an obvious zero rather than a false gap.

Usage:
    scripts/naad_stream_diff.py                     # run until Ctrl-C, human-readable
    scripts/naad_stream_diff.py --log stream.jsonl  # append JSONL as it goes
    scripts/naad_stream_diff.py --duration 86400 --log stream.jsonl
    scripts/naad_stream_diff.py --summary stream.jsonl   # aggregate a finished log

Deploy alongside ``naad_feed_diff.py`` — this imports the GeoRSS fetch and parse
from it, so both probes measure the index the same way.

Exit status:
    0  no alert was missing from both the index and the socket
    1  at least one Actual/Public alert was missing from both (the shared gap)
    2  a GeoRSS host could not be fetched, or the socket never connected

Stdlib only — run with system python3, no venv needed.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import contextlib
import json
import ssl
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    from naad_feed_diff import NEW_HOST, OLD_HOST, Feed, FetchError, fetch
except ImportError:  # pragma: no cover - deployment mistake, not a code path
    sys.exit(
        "naad_stream_diff.py needs naad_feed_diff.py in the same directory "
        "(it reuses its GeoRSS fetch and parser)."
    )

# Mirrors const.py: the surviving-domain TLS stream, the channel the NAADS 2.0
# LMD User Guide documents as correct for 24/7 automated systems.
STREAM_HOST = "streaming.alertready.ca"
STREAM_PORT = 8443
HEARTBEAT_SENDER_PREFIX = "NAADS-Heartbeat"

# Heartbeats arrive at least every 60 s, so silence past this is a dead socket.
# Same value as NAAD_STREAM_HEARTBEAT_TIMEOUT_S.
READ_TIMEOUT_S = 130
BACKOFF_MIN_S = 1
BACKOFF_MAX_S = 60

READ_CHUNK = 65536
MAX_BUFFER_CHARS = 8 * 1024 * 1024
ALERT_START = "<alert"
ALERT_END = "</alert>"

# Sample the index on the cadence the existing probe uses.
DEFAULT_INTERVAL_S = 900
# Alerts published this close to the session start are not held against the
# socket: the connect and the publish may have raced.
DEFAULT_WARMUP_S = 300


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(timespec="seconds")


def parse_ts(raw: str) -> datetime | None:
    """Parse an Atom/CAP timestamp, tolerating the shapes NAAD emits."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    # NAAD writes -00:00 for UTC, which fromisoformat accepts, and occasionally
    # omits the colon in the offset on older feed generators.
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_cap(doc: str) -> dict | None:
    """Pull the identifying and triage fields out of one streamed CAP document.

    Prefers the English ``<info>`` block, matching how the GeoRSS probe picks a
    description, so severities line up across the two sides.
    """
    try:
        root = ET.fromstring(doc)
    except ET.ParseError:
        return None

    identifier = _child_text(root, "identifier")
    if not identifier:
        return None

    infos = [child for child in root if _local(child.tag) == "info"]
    info = None
    for candidate in infos:
        if _child_text(candidate, "language").lower().startswith("en"):
            info = candidate
            break
    if info is None and infos:
        info = infos[0]

    record = {
        "id": identifier,
        "sender": _child_text(root, "sender"),
        "sent": _child_text(root, "sent"),
        "status": _child_text(root, "status"),
        "scope": _child_text(root, "scope"),
        "msg_type": _child_text(root, "msgType"),
        "references": _child_text(root, "references"),
    }
    if info is not None:
        record.update(
            {
                "event": _child_text(info, "event"),
                "severity": _child_text(info, "severity"),
                "urgency": _child_text(info, "urgency"),
                "certainty": _child_text(info, "certainty"),
                "headline": _child_text(info, "headline")[:200],
            }
        )
    return record


def is_heartbeat(doc: str, record: dict | None) -> bool:
    if record is None:
        return ALERT_START in doc and HEARTBEAT_SENDER_PREFIX in doc
    return (
        record.get("sender", "").startswith(HEARTBEAT_SENDER_PREFIX)
        or record.get("status") == "System"
    )


def extract_docs(buffer: str) -> tuple[str, list[str]]:
    """Split complete ``<alert>…</alert>`` frames out of the reassembly buffer.

    Ported from ``providers/naad_stream.py`` so the probe frames the wire exactly
    as the integration does; a framing difference here would be indistinguishable
    from a coverage gap.
    """
    docs: list[str] = []
    while True:
        start = buffer.find(ALERT_START)
        if start == -1:
            if len(buffer) > len(ALERT_START):
                buffer = buffer[-len(ALERT_START) :]
            break
        end = buffer.find(ALERT_END, start)
        if end == -1:
            buffer = buffer[start:]
            break
        end += len(ALERT_END)
        docs.append(buffer[start:end])
        buffer = buffer[end:]
    if len(buffer) > MAX_BUFFER_CHARS:
        buffer = ""
    return buffer, docs


class StreamRecorder:
    """Everything the socket has delivered, plus when it was not listening."""

    def __init__(self) -> None:
        self.session_start: datetime | None = None
        self.alerts: dict[str, dict] = {}
        self.heartbeats = 0
        self.last_heartbeat: datetime | None = None
        self.connects = 0
        self.connect_failures = 0
        self.docs_seen = 0
        self.unparsable = 0
        self.connected = False
        # Windows where nothing could have been received. Left-open until the
        # socket comes back, so an alert published mid-outage is excluded rather
        # than counted against the stream.
        self._offline: list[list[datetime]] = []

    def mark_connected(self) -> None:
        moment = now()
        self.connects += 1
        self.connected = True
        if self.session_start is None:
            self.session_start = moment
        elif self._offline and len(self._offline[-1]) == 1:
            self._offline[-1].append(moment)

    def mark_disconnected(self) -> None:
        if not self.connected:
            return
        self.connected = False
        self._offline.append([now()])

    def note_connect_failure(self) -> None:
        self.connect_failures += 1

    def record_alert(self, doc: str) -> dict | None:
        self.docs_seen += 1
        record = parse_cap(doc)
        if record is None:
            self.unparsable += 1
            return None
        if is_heartbeat(doc, record):
            self.heartbeats += 1
            self.last_heartbeat = now()
            return None
        received = now()
        existing = self.alerts.get(record["id"])
        if existing is not None:
            existing["times_seen"] += 1
            return None
        record["received_at"] = iso(received)
        record["times_seen"] = 1
        self.alerts[record["id"]] = record
        return record

    def offline_at(self, moment: datetime) -> bool:
        for window in self._offline:
            start = window[0]
            end = window[1] if len(window) > 1 else now()
            if start <= moment <= end:
                return True
        return False

    def offline_seconds(self) -> float:
        total = 0.0
        for window in self._offline:
            end = window[1] if len(window) > 1 else now()
            total += (end - window[0]).total_seconds()
        return round(total, 1)

    def state(self) -> dict:
        return {
            "connected": self.connected,
            "session_start": iso(self.session_start),
            "alerts_seen": len(self.alerts),
            "docs_seen": self.docs_seen,
            "heartbeats": self.heartbeats,
            "last_heartbeat": iso(self.last_heartbeat),
            "connects": self.connects,
            "connect_failures": self.connect_failures,
            "unparsable": self.unparsable,
            "offline_s": self.offline_seconds(),
        }


async def stream_loop(
    recorder: StreamRecorder, host: str, port: int, emit, verbose: bool
) -> None:
    """Hold the socket open, reconnecting with bounded backoff, forever."""
    context = await asyncio.to_thread(ssl.create_default_context)
    backoff = float(BACKOFF_MIN_S)
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port, ssl=context)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - transient connect failure
            recorder.note_connect_failure()
            emit(
                {
                    "kind": "connection",
                    "at": iso(now()),
                    "event": "connect_failed",
                    "detail": f"{type(err).__name__}: {err}",
                }
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            continue

        recorder.mark_connected()
        emit({"kind": "connection", "at": iso(now()), "event": "connected"})
        backoff = float(BACKOFF_MIN_S)
        reason = "eof"
        try:
            reason = await read_stream(reader, recorder, emit, verbose)
        except asyncio.CancelledError:
            recorder.mark_disconnected()
            writer.close()
            raise
        except Exception as err:  # noqa: BLE001 - transient read failure
            reason = f"{type(err).__name__}: {err}"
        finally:
            recorder.mark_disconnected()
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=5)

        emit(
            {
                "kind": "connection",
                "at": iso(now()),
                "event": "disconnected",
                "detail": reason,
            }
        )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)


async def read_stream(
    reader: asyncio.StreamReader, recorder: StreamRecorder, emit, verbose: bool
) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    while True:
        try:
            chunk = await asyncio.wait_for(
                reader.read(READ_CHUNK), timeout=READ_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return f"silent for {READ_TIMEOUT_S}s"
        if not chunk:
            return "eof"
        buffer += decoder.decode(chunk)
        buffer, docs = extract_docs(buffer)
        for doc in docs:
            record = recorder.record_alert(doc)
            if record is None:
                continue
            emit({"kind": "stream_alert", **record})
            if verbose:
                print(
                    f"  stream <- {record['id']} "
                    f"[{record.get('status', '')}/{record.get('scope', '')}"
                    f"/{record.get('severity', '')}] {record.get('event', '')}"
                )


def bucket(new: Feed, old: Feed, recorder: StreamRecorder, warmup_s: int) -> dict:
    """Bucket every in-window alert by index presence and stream presence.

    The denominator is the union of both GeoRSS hosts, not pelmorex alone. Almost
    all of alertready's surplus is retention (it keeps ~2 days to pelmorex's ~6
    hours), which the window filter drops anyway, but an alert issued during the
    session that only alertready carries is exactly the reverse failure this
    probe has to be able to see.
    """
    session_start = recorder.session_start
    window_start = (
        None if session_start is None else session_start + timedelta(seconds=warmup_s)
    )
    streamed = set(recorder.alerts)

    counts = {
        "both": 0,
        "rss_gap_stream_has": 0,
        "rss_gap_stream_missing": 0,
        "rss_has_stream_missing": 0,
    }
    gap_has: list[dict] = []
    gap_missing: list[dict] = []
    index_only_missing: list[dict] = []
    pre_session = 0
    offline = 0
    undated = 0

    for alert_id in sorted(old.ids | new.ids):
        # Prefer pelmorex's copy: it is the live feed, and the one whose
        # categories the GeoRSS probe has been reporting all along.
        source = old if alert_id in old.ids else new
        info = source.describe(alert_id)
        updated = parse_ts(info.get("updated", ""))
        if updated is None:
            undated += 1
            continue
        if window_start is None or updated < window_start:
            pre_session += 1
            continue
        if recorder.offline_at(updated):
            offline += 1
            continue

        on_index = alert_id in new.ids
        on_stream = alert_id in streamed
        entry = {
            "id": alert_id,
            "title": info.get("title", ""),
            "severity": info.get("severity", ""),
            "urgency": info.get("urgency", ""),
            "certainty": info.get("certainty", ""),
            "status": info.get("status", ""),
            "scope": info.get("scope", ""),
            "updated": info.get("updated", ""),
            "hosts": "both"
            if alert_id in old.ids and alert_id in new.ids
            else ("pelmorex" if alert_id in old.ids else "alertready"),
        }
        public = info.get("status") == "Actual" and info.get("scope") == "Public"

        if on_index and on_stream:
            counts["both"] += 1
        elif on_index:
            counts["rss_has_stream_missing"] += 1
            if public:
                index_only_missing.append(entry)
        elif on_stream:
            counts["rss_gap_stream_has"] += 1
            if public:
                gap_has.append(entry)
        else:
            counts["rss_gap_stream_missing"] += 1
            if public:
                gap_missing.append(entry)

    considered = sum(counts.values())
    stream_only = [
        alert_id
        for alert_id in streamed
        if alert_id not in old.ids and alert_id not in new.ids
    ]

    return {
        "buckets": counts,
        "window": {
            "start": iso(window_start),
            "considered": considered,
            "excluded_pre_session": pre_session,
            "excluded_offline": offline,
            "excluded_undated": undated,
        },
        # The self-check: if the socket is delivering alerts and none of their
        # ids match pelmorex's, the identity assumption is wrong, not the feed.
        "stream_ids_on_georss": len(streamed & (old.ids | new.ids)),
        "stream_only_total": len(stream_only),
        "rss_gap_stream_has": gap_has,
        "rss_gap_stream_missing": gap_missing,
        "rss_has_stream_missing": index_only_missing,
        "extreme_gap_stream_missing": [
            entry["id"] for entry in gap_missing if entry["severity"] == "Extreme"
        ],
    }


def sample(recorder: StreamRecorder, warmup_s: int) -> dict:
    """Fetch both GeoRSS hosts and compare them against what the socket has."""
    new = fetch(NEW_HOST)
    old = fetch(OLD_HOST)
    record = {
        "kind": "sample",
        "sampled_at": iso(now()),
        "stream": recorder.state(),
        "hosts": {
            "new": {
                "url": new.host,
                "unique_ids": len(new.ids),
                "entries": new.total_entries,
                "fetch_attempts": new.attempts,
            },
            "old": {
                "url": old.host,
                "unique_ids": len(old.ids),
                "entries": old.total_entries,
                "fetch_attempts": old.attempts,
            },
        },
    }
    record.update(bucket(new, old, recorder, warmup_s))
    return record


def print_sample(record: dict) -> None:
    stream = record["stream"]
    window = record["window"]
    counts = record["buckets"]
    state = "connected" if stream["connected"] else "DISCONNECTED"
    print(f"--- {record['sampled_at']}")
    print(
        f"  socket   {state}, {stream['alerts_seen']} alert(s), "
        f"{stream['heartbeats']} heartbeat(s), {stream['connects']} connect(s), "
        f"{stream['offline_s']}s offline"
    )
    print(
        f"  georss   pelmorex {record['hosts']['old']['unique_ids']} ids, "
        f"alertready {record['hosts']['new']['unique_ids']} ids"
    )
    print(
        f"  window   {window['considered']} comparable "
        f"({window['excluded_pre_session']} pre-session, "
        f"{window['excluded_offline']} while offline)"
    )
    if not window["considered"]:
        print("  (nothing published since the session started yet)")
        return
    print(
        f"  buckets  both={counts['both']} "
        f"index-gap/streamed={counts['rss_gap_stream_has']} "
        f"index-gap/NOT-streamed={counts['rss_gap_stream_missing']} "
        f"indexed/not-streamed={counts['rss_has_stream_missing']}"
    )
    if record["stream_ids_on_georss"] == 0 and stream["alerts_seen"]:
        print(
            "  !! none of the streamed ids match a GeoRSS id - check the "
            "identity assumption before reading anything into the buckets"
        )
    for entry in record["rss_gap_stream_missing"]:
        marker = "  !! " if entry["severity"] == "Extreme" else "   - "
        print(f"{marker}missing from BOTH: {entry['id']}")
        print(f"        {entry['title'][:100]}")
        print(f"        severity={entry['severity']} urgency={entry['urgency']}")
    for entry in record["rss_gap_stream_has"]:
        print(f"   + socket covered an index gap: {entry['id']}")
        print(f"        {entry['title'][:100]}")


def summarise(path: str) -> int:
    samples = []
    stream_alerts = 0
    connections: dict[str, int] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            kind = record.get("kind")
            if kind == "sample":
                samples.append(record)
            elif kind == "stream_alert":
                stream_alerts += 1
            elif kind == "connection":
                event = record.get("event", "?")
                connections[event] = connections.get(event, 0) + 1

    if not samples:
        print(f"{path}: no samples yet ({stream_alerts} streamed alert(s) logged)")
        return 0

    totals = {
        "both": 0,
        "rss_gap_stream_has": 0,
        "rss_gap_stream_missing": 0,
        "rss_has_stream_missing": 0,
    }
    # Tolerate a record shape that predates a change to this script: the log on
    # etude accumulates for weeks and must stay summarisable across an edit.
    for record in samples:
        for key in totals:
            totals[key] += record.get("buckets", {}).get(key, 0)

    covered: dict[str, dict] = {}
    shared: dict[str, dict] = {}
    for record in samples:
        for entry in record.get("rss_gap_stream_has", ()):
            covered.setdefault(entry["id"], entry)
        for entry in record.get("rss_gap_stream_missing", ()):
            shared.setdefault(entry["id"], entry)

    last = samples[-1]
    print(f"samples:            {len(samples)}")
    print(f"  first:            {samples[0]['sampled_at']}")
    print(f"  last:             {last['sampled_at']}")
    print(f"  session start:    {last['stream']['session_start']}")
    print(
        f"  streamed alerts:  {stream_alerts} logged, {last['stream']['alerts_seen']} distinct"
    )
    print(f"  heartbeats:       {last['stream']['heartbeats']}")
    print(f"  offline:          {last['stream']['offline_s']}s")
    if connections:
        print(
            "  connections:      "
            + ", ".join(f"{k}={v}" for k, v in sorted(connections.items()))
        )
    print(f"  streamed ids seen on GeoRSS: {last.get('stream_ids_on_georss', 0)}")

    comparable = sum(totals.values())
    print(f"\ncomparable observations: {comparable}")
    for key, value in totals.items():
        share = f"{100 * value / comparable:.1f}%" if comparable else "-"
        print(f"  {key:<24} {value:>6}  {share}")

    print(
        f"\n{len(covered)} distinct alert(s) the index dropped and the SOCKET CARRIED:"
    )
    for entry in covered.values():
        flag = " [EXTREME]" if entry["severity"] == "Extreme" else ""
        print(f"  {entry['id']}{flag}  {entry['title'][:80]}")

    print(f"\n{len(shared)} distinct alert(s) missing from BOTH index and socket:")
    for entry in shared.values():
        flag = " [EXTREME]" if entry["severity"] == "Extreme" else ""
        print(f"  {entry['id']}{flag}  {entry['title'][:80]}")

    print("\nverdict:")
    if not comparable:
        print("  inconclusive — nothing was published inside the observed window")
    elif shared:
        print(
            "  the socket SHARES the index gap. The pelmorex sunset costs users "
            "these alerts."
        )
    elif covered:
        print(
            "  the socket covered every index gap observed. The defect is the "
            "GeoRSS index alone, and the sunset is survivable on streaming."
        )
    else:
        print(
            "  no index gap was observed inside the window — sample longer "
            "before concluding anything."
        )
    return 1 if shared else 0


async def run(args: argparse.Namespace) -> int:
    recorder = StreamRecorder()
    log_handle = open(args.log, "a", encoding="utf-8") if args.log else None

    def emit(record: dict) -> None:
        if log_handle is not None:
            log_handle.write(json.dumps(record) + "\n")
            log_handle.flush()

    stream_task = asyncio.create_task(
        stream_loop(recorder, args.host, args.port, emit, args.verbose)
    )
    deadline = None if not args.duration else now() + timedelta(seconds=args.duration)
    exit_code = 0

    print(
        f"holding {args.host}:{args.port} open; sampling both GeoRSS hosts every "
        f"{args.interval}s (warmup {args.warmup}s). Ctrl-C to stop."
    )
    try:
        while True:
            try:
                record = await asyncio.to_thread(sample, recorder, args.warmup)
            except FetchError as err:
                print(f"fetch failed: {err}", file=sys.stderr)
                exit_code = max(exit_code, 2)
            else:
                emit(record)
                if args.json:
                    print(json.dumps(record))
                else:
                    print_sample(record)
                if record["rss_gap_stream_missing"]:
                    exit_code = max(exit_code, 1)

            if deadline is not None and now() >= deadline:
                break
            await asyncio.sleep(args.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream_task
        if log_handle is not None:
            log_handle.close()

    if recorder.session_start is None:
        print(
            f"the socket never connected ({recorder.connect_failures} failed "
            f"attempt(s)) — nothing was measured",
            file=sys.stderr,
        )
        return 2
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default=STREAM_HOST)
    parser.add_argument("--port", type=int, default=STREAM_PORT)
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_S,
        metavar="SECONDS",
        help=f"GeoRSS sampling cadence (default {DEFAULT_INTERVAL_S})",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=DEFAULT_WARMUP_S,
        metavar="SECONDS",
        help=(
            "ignore alerts published within this long of the session start "
            f"(default {DEFAULT_WARMUP_S})"
        ),
    )
    parser.add_argument(
        "--duration",
        type=int,
        metavar="SECONDS",
        help="stop after roughly this long (default: run until interrupted)",
    )
    parser.add_argument("--log", metavar="PATH", help="append records as JSONL")
    parser.add_argument("--json", action="store_true", help="print records as JSON")
    parser.add_argument(
        "--verbose", action="store_true", help="print every alert the socket delivers"
    )
    parser.add_argument(
        "--summary", metavar="PATH", help="aggregate an existing JSONL log and exit"
    )
    args = parser.parse_args()

    if args.summary:
        return summarise(args.summary)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
