#!/usr/bin/env python3
"""Read-only probe for issue #164: can heartbeat <references> drive repository fetches?

Answers, against the live NAAD endpoints, the three things the issue left open:

1. The exact wire shape of a heartbeat's ``<references>`` (sender, identifier,
   sent triples — and what the ``sent`` looks like, since the URL folds it).
2. The repository URL folding rule, checked two ways: the GeoRSS index's own
   CAP link hrefs vs. a URL constructed from the linked body's sent/identifier,
   and a GET of the constructed URL for every heartbeat reference.
3. Whether the repository is written before the heartbeat lists an alert (a
   GET right after an alert streams, plus one for each heartbeat reference).

Stdlib only. Identifies itself in the User-Agent. Sends nothing but GETs.

Usage:
    naad_repository_probe.py [--duration 180] [--index-samples 5]
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import re
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

STREAM_HOST = "streaming.alertready.ca"
STREAM_PORT = 8443
INDEX_URL = "https://rss.alertready.ca/"
REPO_BASE = "https://cap.alertready.ca"
USER_AGENT = (
    "cap_alerts-probe-164 (read-only; https://github.com/seevee/cap_alerts/issues/164)"
)
NS_ATOM = "{http://www.w3.org/2005/Atom}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(root: ET.Element, name: str) -> str:
    for child in root:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def fold(value: str) -> str:
    """The NAADS repository filename folding: ':' '-' '+' → '_'."""
    return re.sub(r"[:\-+]", "_", value)


def repo_url(sent: str, identifier: str) -> str:
    """https://cap.alertready.ca/{YYYY-MM-DD}/{fold(sent)}I{fold(identifier)}.xml"""
    day = sent[:10]
    return f"{REPO_BASE}/{day}/{fold(sent)}I{fold(identifier)}.xml"


def get(url: str, timeout: int = 15) -> tuple[int, int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, len(body), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, 0, ""
    except Exception as err:  # noqa: BLE001
        return -1, 0, f"{type(err).__name__}: {err}"


def parse_refs(text: str) -> list[tuple[str, str, str]]:
    refs = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 3:
            continue
        refs.append((parts[0], ",".join(parts[1:-1]), parts[-1]))
    return refs


def check_index(samples: int) -> None:
    """Index link hrefs vs. URLs constructed from the linked CAP body."""
    print(f"== index check: {INDEX_URL}")
    text = ""
    for attempt in range(3):
        status, size, text = get(INDEX_URL, timeout=60)
        if status == 200 and text.rstrip().endswith("</feed>"):
            break
        print(f"   attempt {attempt + 1}: status={status} size={size} (truncated?)")
    else:
        print("   could not fetch a complete index; skipping")
        return
    root = ET.fromstring(text)
    entries = root.findall(f"{NS_ATOM}entry")
    print(f"   {len(entries)} entries")
    checked = 0
    for entry in entries:
        href = ""
        for link in entry.findall(f"{NS_ATOM}link"):
            h = link.get("href", "")
            if h.lower().endswith(".xml"):
                href = h
                break
        if not href:
            continue
        status, size, body = get(href)
        if status != 200:
            print(f"   {href}: HTTP {status}")
            continue
        doc = ET.fromstring(body)
        sent = _child(doc, "sent")
        identifier = _child(doc, "identifier")
        built = repo_url(sent, identifier)
        ok = built == href
        print(f"   {'OK  ' if ok else 'DIFF'} sent={sent} id={identifier}")
        if not ok:
            print(f"        href : {href}")
            print(f"        built: {built}")
        checked += 1
        if checked >= samples:
            break


async def watch_stream(duration: int) -> None:
    print(f"== stream: {STREAM_HOST}:{STREAM_PORT} for {duration}s")
    ctx = await asyncio.to_thread(ssl.create_default_context)
    reader, writer = await asyncio.open_connection(STREAM_HOST, STREAM_PORT, ssl=ctx)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    heartbeats = 0
    printed_raw = False
    seen_ref_ids: set[str] = set()
    try:
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(65536), timeout=max(1, deadline - loop.time())
                )
            except asyncio.TimeoutError:
                break
            if not chunk:
                print("   EOF")
                break
            buffer += decoder.decode(chunk)
            while True:
                start = buffer.find("<alert")
                if start == -1:
                    break
                end = buffer.find("</alert>", start)
                if end == -1:
                    buffer = buffer[start:]
                    break
                end += len("</alert>")
                doc_str = buffer[start:end]
                buffer = buffer[end:]
                await handle_doc(doc_str, seen_ref_ids, heartbeats, printed_raw)
                if (
                    "NAADS-Heartbeat" in doc_str[:600]
                    or "<status>System</status>" in doc_str
                ):
                    heartbeats += 1
                    printed_raw = True
    finally:
        writer.close()
    print(f"   done: {heartbeats} heartbeat(s)")


async def handle_doc(
    doc_str: str, seen_ref_ids: set[str], heartbeats: int, printed_raw: bool
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        root = ET.fromstring(doc_str)
    except ET.ParseError as err:
        print(f"   [{now}] unparsable frame: {err}")
        return
    sender = _child(root, "sender")
    status = _child(root, "status")
    identifier = _child(root, "identifier")
    sent = _child(root, "sent")
    refs_text = _child(root, "references")
    is_hb = sender.startswith("NAADS-Heartbeat") or status == "System"
    if is_hb:
        if not printed_raw:
            print(f"   [{now}] first heartbeat, raw (first 1500 chars):")
            print("   " + doc_str[:1500].replace("\n", "\n   "))
        refs = parse_refs(refs_text)
        print(f"   [{now}] heartbeat id={identifier} sent={sent} refs={len(refs)}")
        for ref_sender, ref_id, ref_sent in refs:
            if ref_id in seen_ref_ids:
                continue
            seen_ref_ids.add(ref_id)
            url = repo_url(ref_sent, ref_id)
            st, size, body = await asyncio.to_thread(get, url)
            verdict = "OK " if st == 200 else "!! "
            extra = ""
            if st == 200:
                d = ET.fromstring(body)
                extra = f" body.id={'match' if _child(d, 'identifier') == ref_id else 'MISMATCH'} status={_child(d, 'status')}"
            print(
                f"      {verdict}{st} {size:>7}B sender={ref_sender} sent={ref_sent} id={ref_id}{extra}"
            )
            if st != 200:
                print(f"         {url}")
        return
    # A live alert: is it already in the repository?
    url = repo_url(sent, identifier)
    st, size, _ = await asyncio.to_thread(get, url)
    print(
        f"   [{now}] ALERT streamed id={identifier} sent={sent} status={status} msgType={_child(root, 'msgType')}"
    )
    print(f"      immediate repository GET: {st} {size}B {url}")
    if st != 200:
        await asyncio.sleep(5)
        st, size, _ = await asyncio.to_thread(get, url)
        print(f"      retry after 5s: {st} {size}B")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--index-samples", type=int, default=5)
    parser.add_argument("--skip-index", action="store_true")
    args = parser.parse_args()
    print(
        f"probe start {datetime.now(timezone.utc).isoformat(timespec='seconds')} UA={USER_AGENT}"
    )
    if not args.skip_index:
        await asyncio.to_thread(check_index, args.index_samples)
    await watch_stream(args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
