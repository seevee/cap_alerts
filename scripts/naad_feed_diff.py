#!/usr/bin/env python3
"""Diff the two NAAD GeoRSS hosts to characterise coverage gaps (issue #38).

Fetches rss.alertready.ca (the sanctioned endpoint, per the March 25 2026
Governance Council summary) and rss.naad-adna.pelmorex.com (the legacy host,
sunsetting ~Sept 2026) concurrently, then reports alerts present on one host
and absent from the other.

A 2026-07-22 sample found six status=Actual/scope=Public alerts on the legacy
host and missing from alertready, including an Extreme BC Emergency Alert.
That was a single observation; this script exists to establish whether the gap
is systematic. Run it on a schedule and let the JSONL log accumulate.

Both hosts are also checked for the truncation the integration guards against
(chunked, no Content-Length -> partial body with no transport error), since a
truncated fetch would otherwise masquerade as a coverage gap.

Usage:
    scripts/naad_feed_diff.py                        # one comparison, human-readable
    scripts/naad_feed_diff.py --log probe.jsonl      # append one sample record
    scripts/naad_feed_diff.py --interval 900 --log probe.jsonl   # sample until Ctrl-C
    scripts/naad_feed_diff.py --summary probe.jsonl  # aggregate a finished log

Exit status:
    0  no alerts missing from alertready
    1  alerts missing from alertready (cron-friendly: non-zero means "gap seen")
    2  a host could not be fetched or parsed

Stdlib only — run with system python3, no venv needed.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ATOM = {"a": "http://www.w3.org/2005/Atom"}

NEW_HOST = "https://rss.alertready.ca/"
OLD_HOST = "https://rss.naad-adna.pelmorex.com/"

# Mirrors _FEED_FETCH_ATTEMPTS / _FEED_RETRY_BACKOFF_S in providers/eccc.py.
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.5

USER_AGENT = "HomeAssistant-CAPAlerts/feed-probe (issue-38 coverage diff)"

# Headers worth recording: the transport differences reported upstream.
HEADERS_OF_INTEREST = (
    "content-length",
    "transfer-encoding",
    "content-encoding",
    "etag",
    "last-modified",
    "server",
)


class FetchError(RuntimeError):
    """A host could not be fetched, or served a truncated body every attempt."""


class Feed:
    """One host's parsed feed, keyed by the per-alert portion of the Atom id."""

    def __init__(self, host: str, body: bytes, headers: dict, attempts: int) -> None:
        self.host = host
        self.size = len(body)
        self.headers = headers
        self.attempts = attempts
        self.entries: dict[str, list[dict]] = {}
        self.training_tagged = 0
        self.total_entries = 0

        root = ET.fromstring(body)
        for entry in root.findall("a:entry", ATOM):
            raw_id = entry.findtext("a:id", default="", namespaces=ATOM)
            # Ids are tag:<host>,<date>:feed.atom/<uuid-or-oid>; the host prefix
            # differs between feeds by design, so compare only the suffix.
            alert_id = raw_id.rsplit("feed.atom/", 1)[-1]
            cats = {}
            for cat in entry.findall("a:category", ATOM):
                term = cat.get("term", "")
                if "=" in term:
                    key, _, value = term.partition("=")
                    cats[key] = value
            self.entries.setdefault(alert_id, []).append(
                {
                    "title": entry.findtext("a:title", default="", namespaces=ATOM),
                    "updated": entry.findtext("a:updated", default="", namespaces=ATOM),
                    **cats,
                }
            )
            self.total_entries += 1
            if "rsstrainingdqs" in raw_id:
                self.training_tagged += 1

    @property
    def ids(self) -> set[str]:
        return set(self.entries)

    def describe(self, alert_id: str) -> dict:
        """Best single description of an alert, preferring its en-CA variant."""
        versions = self.entries[alert_id]
        for version in versions:
            if version.get("language", "").startswith("en"):
                return version
        return versions[0]


def fetch(url: str) -> Feed:
    """Fetch one host, retrying truncated bodies the way the provider does."""
    last_error = "unknown"
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                body = resp.read()
                headers = {
                    key: resp.headers.get(key)
                    for key in HEADERS_OF_INTEREST
                    if resp.headers.get(key) is not None
                }
        except (urllib.error.URLError, OSError) as err:
            last_error = f"{type(err).__name__}: {err}"
        else:
            # The integration's own guard: a cut-short chunked response arrives
            # without a transport error, so completeness has to be checked here.
            if body.rstrip().endswith(b"</feed>"):
                try:
                    return Feed(url, body, headers, attempt)
                except ET.ParseError as err:
                    last_error = f"ParseError: {err}"
            else:
                last_error = f"truncated body ({len(body)} bytes, no closing </feed>)"
        if attempt < FETCH_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_S)
    raise FetchError(f"{url}: {last_error} after {FETCH_ATTEMPTS} attempts")


def compare(new: Feed, old: Feed) -> dict:
    """Build one sample record. 'missing' is the direction that matters."""
    missing_from_new = sorted(old.ids - new.ids)
    missing_from_old = sorted(new.ids - old.ids)

    def public_actual(feed: Feed, ids: list[str]) -> list[dict]:
        out = []
        for alert_id in ids:
            info = feed.describe(alert_id)
            if info.get("status") == "Actual" and info.get("scope") == "Public":
                out.append(
                    {
                        "id": alert_id,
                        "title": info.get("title", ""),
                        "severity": info.get("severity", ""),
                        "urgency": info.get("urgency", ""),
                        "certainty": info.get("certainty", ""),
                        "event": info.get("event", ""),
                        "updated": info.get("updated", ""),
                    }
                )
        return out

    gaps = public_actual(old, missing_from_new)
    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hosts": {
            "new": _host_record(new),
            "old": _host_record(old),
        },
        # Alerts the sanctioned endpoint is not serving but the legacy one is.
        "missing_from_new": gaps,
        "missing_from_new_total": len(missing_from_new),
        # Expected to be large: alertready retains far more history.
        "missing_from_old_total": len(missing_from_old),
        "extreme_missing": [g["id"] for g in gaps if g["severity"] == "Extreme"],
    }


def _host_record(feed: Feed) -> dict:
    return {
        "url": feed.host,
        "bytes": feed.size,
        "entries": feed.total_entries,
        "unique_ids": len(feed.ids),
        "training_tagged": feed.training_tagged,
        "fetch_attempts": feed.attempts,
        "headers": feed.headers,
    }


def sample() -> dict:
    """Fetch both hosts concurrently so neither feed is a moment staler."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        new_future = pool.submit(fetch, NEW_HOST)
        old_future = pool.submit(fetch, OLD_HOST)
        return compare(new_future.result(), old_future.result())


def print_record(record: dict) -> None:
    for label, key in (("alertready", "new"), ("pelmorex  ", "old")):
        host = record["hosts"][key]
        tagged = ""
        if host["training_tagged"]:
            tagged = f", {host['training_tagged']}/{host['entries']} rsstrainingdqs"
        retried = ""
        if host["fetch_attempts"] > 1:
            retried = f", {host['fetch_attempts']} attempts (truncation retried)"
        print(
            f"{label}  {host['bytes']:>9,} B  "
            f"{host['unique_ids']:>4} ids  {host['entries']:>5} entries{tagged}{retried}"
        )
        missing_headers = [
            h for h in ("content-length", "etag") if h not in host["headers"]
        ]
        if missing_headers:
            print(f"             no {', '.join(missing_headers)}")

    print()
    gaps = record["missing_from_new"]
    if not gaps:
        print(
            f"No Actual/Public alerts missing from alertready "
            f"({record['missing_from_new_total']} id(s) differed, none public/actual)."
        )
        return

    print(f"{len(gaps)} Actual/Public alert(s) on pelmorex and ABSENT from alertready:")
    for gap in gaps:
        marker = "  !! " if gap["severity"] == "Extreme" else "   - "
        print(f"{marker}{gap['id']}")
        print(f"        {gap['title'][:100]}")
        print(
            f"        severity={gap['severity']} urgency={gap['urgency']} "
            f"certainty={gap['certainty']} updated={gap['updated']}"
        )


def summarise(path: str) -> int:
    """Aggregate a JSONL log: how often the gap appears, and for which alerts."""
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        print(f"{path}: no samples")
        return 0

    with_gaps = [r for r in records if r["missing_from_new"]]
    seen: dict[str, dict] = {}
    for record in records:
        for gap in record["missing_from_new"]:
            entry = seen.setdefault(
                gap["id"],
                {
                    "count": 0,
                    "first": record["sampled_at"],
                    "severity": gap["severity"],
                    "title": gap["title"],
                },
            )
            entry["count"] += 1
            entry["last"] = record["sampled_at"]

    print(f"samples:          {len(records)}")
    print(f"  first:          {records[0]['sampled_at']}")
    print(f"  last:           {records[-1]['sampled_at']}")
    print(
        f"  with gaps:      {len(with_gaps)} "
        f"({100 * len(with_gaps) / len(records):.0f}%)"
    )

    retries = collections.Counter()
    for record in records:
        for key in ("new", "old"):
            if record["hosts"][key]["fetch_attempts"] > 1:
                retries[key] += 1
    if retries:
        print(
            "  truncation retries: "
            + ", ".join(f"{k}={v}" for k, v in sorted(retries.items()))
        )

    if not seen:
        print("\nNo Actual/Public alerts were ever missing from alertready.")
        return 0

    print(f"\n{len(seen)} distinct alert(s) missing from alertready across the log:")
    for alert_id, info in sorted(seen.items(), key=lambda kv: -kv[1]["count"]):
        flag = " [EXTREME]" if info["severity"] == "Extreme" else ""
        print(f"  {alert_id}{flag}")
        print(f"    {info['title'][:100]}")
        print(
            f"    seen in {info['count']} sample(s), {info['first']} -> {info['last']}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--log", metavar="PATH", help="append each sample as JSONL")
    parser.add_argument(
        "--interval",
        type=int,
        metavar="SECONDS",
        help="keep sampling every SECONDS until interrupted",
    )
    parser.add_argument("--json", action="store_true", help="print the record as JSON")
    parser.add_argument(
        "--summary", metavar="PATH", help="aggregate an existing JSONL log and exit"
    )
    args = parser.parse_args()

    if args.summary:
        return summarise(args.summary)

    exit_code = 0
    while True:
        try:
            record = sample()
        except FetchError as err:
            print(f"fetch failed: {err}", file=sys.stderr)
            if not args.interval:
                return 2
            exit_code = 2
        else:
            if args.json:
                print(json.dumps(record))
            else:
                print(f"--- {record['sampled_at']}")
                print_record(record)
            if args.log:
                with open(args.log, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
            if record["missing_from_new"] and exit_code == 0:
                exit_code = 1

        if not args.interval:
            return exit_code
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return exit_code


if __name__ == "__main__":
    sys.exit(main())
