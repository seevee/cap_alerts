#!/usr/bin/env python3
"""What would ``RestoreEntity`` put in ``core.restore_state`` for alert entities?

RFC §2.5 has an incident entity inherit ``RestoreEntity``. Home Assistant
writes one record per restorable entity to ``.storage/core.restore_state``,
the whole file every 15 minutes and at stop, and keeps the record of a removed
entity for seven days (``helpers/restore_state.py``). The alert entities do
not inherit ``RestoreEntity`` today, so this is a projection: the records they
would add, sized from a running instance.

The unit is the config entry, because that is what one household runs. A
development instance carries many entries at once and its total describes
nobody's install, so the total is printed last and labelled as such.

Sources, all read-only:

    /api/states       every live alert entity's state. A record is that state
                      as compact JSON inside a three-key envelope, which is
                      how the file stores it.
    --storage         ``core.entity_registry`` attributes an entity to its
                      entry, and its ``deleted_entities`` say which alert
                      entities were removed in the last seven days
                      (``modified_at``). ``core.config_entries`` gives titles.
                      ``core.restore_state`` is the file as it is now.

A removed alert's state is gone, so each one is sized at the mean live record
of its entry, or of all entries when the entry has none live. Those rows are
marked ``*``.

Usage:
    scripts/restore_state_size.py --storage config/.storage
    scripts/restore_state_size.py --storage ... --url http://ha.lan:8123 --token "$TOKEN"
    scripts/restore_state_size.py --storage ... --json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flow_walk import mint_token

ALERT_MARK = "_cap_alert_"
# homeassistant/helpers/restore_state.py
STATE_DUMP_INTERVAL = timedelta(minutes=15)
STATE_EXPIRATION = timedelta(days=7)
DUMPS_PER_DAY = timedelta(days=1) // STATE_DUMP_INTERVAL


def _is_alert(entity: dict) -> bool:
    return entity.get("platform") == "cap_alerts" and ALERT_MARK in entity["entity_id"]


def _record_bytes(state: dict, now: datetime) -> int:
    record = {"state": state, "extra_data": None, "last_seen": now.isoformat()}
    return len(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode())


def _states(url: str, token: str) -> list[dict]:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/states",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def measure(states: list[dict], storage: Path, now: datetime) -> dict:
    registry = json.loads((storage / "core.entity_registry").read_text())["data"]
    config = json.loads((storage / "core.config_entries").read_text())
    titles = {
        e["entry_id"]: e["title"]
        for e in config["data"]["entries"]
        if e["domain"] == "cap_alerts"
    }
    owner = {
        e["entity_id"]: e["config_entry_id"]
        for e in registry["entities"]
        if _is_alert(e)
    }

    live: dict[str, list[int]] = defaultdict(list)
    for state in states:
        entry_id = owner.get(state["entity_id"])
        if entry_id in titles and state["state"] != "unavailable":
            live[entry_id].append(_record_bytes(state, now))
    sizes = [size for per_entry in live.values() for size in per_entry]
    overall_mean = statistics.mean(sizes) if sizes else 0.0

    cutoff = now - STATE_EXPIRATION
    removed: Counter[str] = Counter(
        e["config_entry_id"]
        for e in registry["deleted_entities"]
        if _is_alert(e)
        and e.get("config_entry_id") in titles
        and datetime.fromisoformat(e["modified_at"]) >= cutoff
    )

    entries = []
    for entry_id, title in sorted(titles.items(), key=lambda item: item[1]):
        records = live.get(entry_id, [])
        mean = statistics.mean(records) if records else overall_mean
        live_bytes = sum(records)
        lingering_bytes = round(removed[entry_id] * mean)
        file_bytes = live_bytes + lingering_bytes
        entries.append(
            {
                "entry_id": entry_id,
                "title": title,
                "live": len(records),
                "live_bytes": live_bytes,
                "mean_record_bytes": round(mean),
                "mean_is_overall": not records,
                "removed_7d": removed[entry_id],
                "lingering_bytes": lingering_bytes,
                "file_bytes": file_bytes,
                "bytes_per_day": file_bytes * DUMPS_PER_DAY,
            }
        )

    restore_file = storage / "core.restore_state"
    current = json.loads(restore_file.read_text())["data"]
    return {
        "observed": now.isoformat(timespec="minutes"),
        "live_records": len(sizes),
        "record_bytes_mean": round(overall_mean),
        "record_bytes_median": round(statistics.median(sizes)) if sizes else 0,
        "record_bytes_max": max(sizes, default=0),
        "restore_state_bytes": restore_file.stat().st_size,
        "restore_state_records": len(current),
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--url", default="http://localhost:8123")
    parser.add_argument("--container", default="weather-alerts-card-ha")
    parser.add_argument("--token", default=None, help="overrides $HA_TOKEN")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    token = args.token or os.environ.get("HA_TOKEN") or mint_token(args.container)
    result = measure(_states(args.url, token), args.storage, datetime.now(UTC))
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0
    print(
        f"observed {result['observed']}, {result['live_records']} live alert "
        f"entities, record bytes mean {result['record_bytes_mean']} "
        f"median {result['record_bytes_median']} max {result['record_bytes_max']}"
    )
    head = ("entry", "live", "live KB", "mean B", "removed 7d", "linger KB")
    head += ("file KB", "MB/day")
    print("{:<44}{:>6}{:>9}{:>9}{:>11}{:>11}{:>9}{:>9}".format(*head))
    for e in result["entries"]:
        mean = f"{e['mean_record_bytes']}{'*' if e['mean_is_overall'] else ''}"
        print(
            f"{e['title'][:43]:<44}{e['live']:>6}{e['live_bytes'] / 1e3:>9.1f}"
            f"{mean:>9}{e['removed_7d']:>11}{e['lingering_bytes'] / 1e3:>11.1f}"
            f"{e['file_bytes'] / 1e3:>9.1f}{e['bytes_per_day'] / 1e6:>9.2f}"
        )
    print(
        f"core.restore_state is {result['restore_state_bytes']} bytes, "
        f"{result['restore_state_records']} records, none of them alert entities"
    )
    file_bytes = sum(e["file_bytes"] for e in result["entries"])
    print(
        f"all entries together (no one's install): {file_bytes / 1e3:.1f} KB added, "
        f"{file_bytes * DUMPS_PER_DAY / 1e6:.1f} MB a day"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
