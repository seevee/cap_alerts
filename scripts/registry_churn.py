#!/usr/bin/env python3
"""How much entity-registry traffic does one entity per alert cost?

RFC §2.5 accepts registry churn at incident boundaries as the price of the
entity binding. This measures that price from a Home Assistant recorder
database: for each ``cap_alerts`` config entry, how many alert entities were
created and removed, how many were registered at once, and how busy the
busiest hour was.

The unit is the config entry, because that is what one household runs. A
development instance carries many entries at once and its total describes
nobody's install, so the total is printed last and labelled as such.

Sources, all read-only:

    events            ``entity_registry_updated`` rows with action ``create``
                      or ``remove`` for alert entities. Registry entries
                      survive a restart, so a restart adds nothing here.
    events            ``incident_*`` rows, which carry ``entry_id`` beside
                      ``entity_id`` and so attribute an entity to its entry.
    --storage         ``core.entity_registry`` and ``core.config_entries``,
                      for entities with no event in the window and for titles.
                      Without it, concurrency is relative to the window's
                      lowest point and entries print as ids.

The registry file is also where the lasting cost shows. Home Assistant keeps a
``deleted_entities`` record for every removed entity so a returning unique id
gets its entity id and settings back, and purges one only after its config
entry is gone (``ORPHANED_ENTITY_KEEP_SECONDS``, 30 days). An alert entity is
removed while its entry lives on, so its record stays. With ``--storage`` the
report counts those records per entry, their bytes, and removals per month
from their ``modified_at``, which reaches back past the recorder's window.

Only whole UTC days inside the recorder's window count toward the per-day
figures. The recorder keeps ten days by default.

Usage:
    scripts/registry_churn.py --db config/home-assistant_v2.db
    scripts/registry_churn.py --db ... --storage config/.storage
    scripts/registry_churn.py --db ... --storage ... --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ALERT_MARK = "_cap_alert_"
INCIDENT_EVENTS = ("incident_created", "incident_updated", "incident_removed")
DAY = 86400
HOUR = 3600


def _rows(db: sqlite3.Connection, event_types: tuple[str, ...]) -> list[tuple]:
    marks = ",".join("?" for _ in event_types)
    return db.execute(
        "SELECT t.event_type, e.time_fired_ts, d.shared_data "
        "FROM events e "
        "JOIN event_types t USING (event_type_id) "
        "JOIN event_data d USING (data_id) "
        f"WHERE t.event_type IN ({marks}) ORDER BY e.time_fired_ts",
        event_types,
    ).fetchall()


def _is_alert(entity: dict) -> bool:
    return entity.get("platform") == "cap_alerts" and ALERT_MARK in entity["entity_id"]


def _storage(path: Path | None) -> dict:
    """Read what ``.storage`` knows: live owners, tombstones, titles, file size."""
    if path is None:
        return {"owners": {}, "tombstones": [], "titles": {}, "registry_bytes": None}
    registry_file = path / "core.entity_registry"
    registry = json.loads(registry_file.read_text())["data"]
    entries = json.loads((path / "core.config_entries").read_text())
    return {
        "owners": {
            e["entity_id"]: e["config_entry_id"]
            for e in registry["entities"]
            if _is_alert(e)
        },
        "tombstones": [e for e in registry["deleted_entities"] if _is_alert(e)],
        "titles": {
            e["entry_id"]: e["title"]
            for e in entries["data"]["entries"]
            if e["domain"] == "cap_alerts"
        },
        "registry_bytes": registry_file.stat().st_size,
    }


def measure(db_path: Path, storage: Path | None) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    stored = _storage(storage)
    registered_now, titles = stored["owners"], stored["titles"]
    owner: dict[str, str] = dict(registered_now)
    tombstones: dict[str, list[dict]] = defaultdict(list)
    for tombstone in stored["tombstones"]:
        if entry_id := tombstone.get("config_entry_id"):
            owner.setdefault(tombstone["entity_id"], entry_id)
            tombstones[entry_id].append(tombstone)
    updates: Counter[str] = Counter()
    for event_type, _, raw in _rows(db, INCIDENT_EVENTS):
        data = json.loads(raw)
        entry_id = data.get("entry_id")
        if entry_id and data.get("entity_id"):
            owner[data["entity_id"]] = entry_id
        if entry_id and event_type == "incident_updated":
            updates[entry_id] += 1

    first, last = db.execute(
        "SELECT MIN(time_fired_ts), MAX(time_fired_ts) FROM events"
    ).fetchone()
    days = range(int(first // DAY) + 1, int(last // DAY))

    ops: dict[str, list[tuple[float, int]]] = defaultdict(list)
    unattributed = 0
    for _, ts, raw in _rows(db, ("entity_registry_updated",)):
        data = json.loads(raw)
        step = {"create": 1, "remove": -1}.get(data.get("action"))
        entity_id = data.get("entity_id", "")
        if step is None or ALERT_MARK not in entity_id:
            continue
        if entity_id not in owner:
            unattributed += 1
            continue
        ops[owner[entity_id]].append((ts, step))

    now_count = Counter(registered_now.values())
    entries = []
    for entry_id in sorted(set(ops) | set(titles), key=lambda e: titles.get(e, e)):
        entry_ops = ops.get(entry_id, [])
        kept = tombstones.get(entry_id, [])
        per_day = Counter(int(ts // DAY) for ts, _ in entry_ops)
        per_hour = Counter(int(ts // HOUR) for ts, _ in entry_ops)
        daily = [per_day[day] for day in days]
        # Walk the window backwards from what is registered now. Without
        # --storage the anchor is unknown, so shift the walk to a floor of 0.
        level = now_count[entry_id]
        peak = low = level
        for _, step in reversed(entry_ops):
            level -= step
            peak, low = max(peak, level), min(low, level)
        if storage is None:
            peak -= low
        busiest = max(per_hour.items(), key=lambda kv: kv[1], default=(None, 0))
        entries.append(
            {
                "entry_id": entry_id,
                "title": titles.get(entry_id, entry_id[:8]),
                "created": sum(1 for _, s in entry_ops if s > 0),
                "removed": sum(1 for _, s in entry_ops if s < 0),
                "writes_per_day_median": statistics.median(daily) if daily else 0,
                "writes_per_day_max": max(daily, default=0),
                "busiest_hour_writes": busiest[1],
                "busiest_hour": (
                    datetime.fromtimestamp(busiest[0] * HOUR, UTC).isoformat()
                    if busiest[0] is not None
                    else None
                ),
                "peak_registered": peak,
                "registered_now": now_count.get(entry_id) if storage else None,
                "updates": updates.get(entry_id, 0),
                "tombstones": len(kept) if storage else None,
                "tombstone_bytes": (
                    sum(len(json.dumps(t)) for t in kept) if storage else None
                ),
                "removed_by_month": dict(
                    sorted(Counter(t["modified_at"][:7] for t in kept).items())
                ),
            }
        )
    return {
        "window": [
            datetime.fromtimestamp(first, UTC).isoformat(timespec="minutes"),
            datetime.fromtimestamp(last, UTC).isoformat(timespec="minutes"),
        ],
        "whole_days": len(days),
        "unattributed_registry_writes": unattributed,
        "registry_bytes": stored["registry_bytes"],
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = measure(args.db, args.storage)
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0
    print(
        f"window {result['window'][0]} to {result['window'][1]}, "
        f"{result['whole_days']} whole days, "
        f"{result['unattributed_registry_writes']} registry writes unattributed"
    )
    head = ("entry", "created", "removed", "med/day", "max/day", "max/hour")
    head += ("peak", "tombs", "KB")
    print("{:<44}{:>8}{:>8}{:>8}{:>8}{:>9}{:>6}{:>8}{:>8}".format(*head))
    for e in result["entries"]:
        kept = "" if e["tombstones"] is None else e["tombstones"]
        size = (
            "" if e["tombstone_bytes"] is None else round(e["tombstone_bytes"] / 1024)
        )
        print(
            f"{e['title'][:43]:<44}{e['created']:>8}{e['removed']:>8}"
            f"{e['writes_per_day_median']:>8g}{e['writes_per_day_max']:>8}"
            f"{e['busiest_hour_writes']:>9}{e['peak_registered']:>6}"
            f"{kept:>8}{size:>8}"
        )
    if result["registry_bytes"] is not None:
        print(f"core.entity_registry is {result['registry_bytes']} bytes")
    created = sum(e["created"] for e in result["entries"])
    removed = sum(e["removed"] for e in result["entries"])
    print(
        f"all entries together (no one's install): {created} created, {removed} removed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
