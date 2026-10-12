#!/usr/bin/env python3
"""Read-only probe of the cap-sources bucket feeds behind the WMO registry.

For every registry record whose ``capAlertFeed`` lives on the cap-sources S3
bucket, fetch the bucket's ``rss.xml`` and report what the integration would
see if it polled the authority feed instead of the SWIC mirror (issue #210):
item count, newest build date, whether items carry ``cap:`` extensions, and
for the listed items the CAP ``status`` and whether ``expires`` is still in
the future. Stdlib only, identifies itself, writes nothing upstream.

    scripts/wmo_bucket_probe.py [--limit N] [--bodies N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

REGISTRY = "https://severeweather.wmo.int/v2/json/sources.json"
BUCKET = "https://cap-sources.s3.amazonaws.com/"
UA = "cap_alerts-wmo-bucket-probe/1 (read-only; github.com/seevee/cap_alerts)"


def fetch(url: str, timeout: float = 20) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                dict(resp.headers),
                resp.read().decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), ""


def folders(registry: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for entry in registry["sources"]:
        src = entry["source"]
        for url in str(src.get("capAlertFeed") or "").split(","):
            m = re.match(rf"{re.escape(BUCKET)}([^/]+)/rss\.xml$", url.strip())
            if m:
                out.setdefault(m.group(1), []).append(src["sourceId"])
    return out


def parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def probe(folder: str, bodies: int) -> dict:
    status, headers, text = fetch(f"{BUCKET}{folder}/rss.xml")
    row: dict = {
        "folder": folder,
        "http": status,
        "items": 0,
        "cap_ns": False,
        "last_build": None,
        "cache_control": headers.get("Cache-Control"),
        "live": 0,
        "expired": 0,
        "no_expires": 0,
        "status": Counter(),
        "guid_like_url": None,
    }
    if status != 200:
        return row
    row["cap_ns"] = "urn:oasis:names:tc:emergency:cap" in text
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        row["http"] = "parse-error"
        return row
    row["last_build"] = root.findtext("channel/lastBuildDate")
    items = list(root.iter("item"))
    row["items"] = len(items)
    now = datetime.now(timezone.utc)
    for item in items[:bodies]:
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        same = guid == link
        row["guid_like_url"] = (
            same if row["guid_like_url"] is None else row["guid_like_url"] and same
        )
        st, _, body = fetch(link)
        if st != 200:
            row["status"][f"fetch-{st}"] += 1
            continue
        try:
            doc = ET.fromstring(body)
        except ET.ParseError:
            row["status"]["parse-error"] += 1
            continue
        row["status"][(doc.findtext("{*}status") or "?").strip()] += 1
        row["bytes"] = row.get("bytes", 0) + len(body.encode())
        sent = parse_iso(doc.findtext("{*}sent") or "")
        if sent and (row.get("newest_sent") is None or sent > row["newest_sent"]):
            row["newest_sent"] = sent
        expires = [parse_iso(e.text or "") for e in doc.findall(".//{*}expires")]
        expires = [e for e in expires if e]
        if not expires:
            row["no_expires"] += 1
        elif max(expires) > now:
            row["live"] += 1
        else:
            row["expired"] += 1
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit", type=int, default=0, help="probe only the first N folders"
    )
    ap.add_argument("--bodies", type=int, default=5, help="CAP bodies to read per feed")
    args = ap.parse_args()
    _, _, text = fetch(REGISTRY)
    fold = folders(json.loads(text))
    names = sorted(fold)[: args.limit or None]
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(lambda f: probe(f, args.bodies), names))
    http = Counter(r["http"] for r in rows)
    with_items = [r for r in rows if r["items"]]
    print(
        f"folders {len(rows)}  http {dict(http)}  with items {len(with_items)}  cap-ns anywhere {sum(r['cap_ns'] for r in rows)}"
    )
    print(f"cache-control values {Counter(r['cache_control'] for r in rows)}")
    status = Counter()
    live = expired = noexp = 0
    for r in with_items:
        status.update(r["status"])
        live += r["live"]
        expired += r["expired"]
        noexp += r["no_expires"]
    print(
        f"bodies read {sum(status.values())}  status {dict(status)}  live {live} expired {expired} no-expires {noexp}"
    )
    print(
        f"guid == link on every item: {all(r['guid_like_url'] for r in with_items if r['guid_like_url'] is not None)}"
    )
    print()
    print(
        f"{'folder':20} {'items':>5} {'live':>4} {'exp':>4} {'noexp':>5} {'KB':>5} newest_sent          last_build"
    )
    for r in sorted(with_items, key=lambda r: -r["items"]):
        newest = r.get("newest_sent")
        ns = newest.strftime("%Y-%m-%d %H:%M") if newest else "-"
        print(
            f"{r['folder']:20} {r['items']:>5} {r['live']:>4} {r['expired']:>4} {r['no_expires']:>5} {r.get('bytes', 0) // 1024:>5} {ns:20} {r['last_build']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
