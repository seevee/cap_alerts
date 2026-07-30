#!/usr/bin/env python3
"""Sample the MeteoAlarm France feed to validate the episode-merge assumptions.

Background: MeteoFrance publishes one warning per calendar day and issues the
next day's bulletin in the afternoon, so a multi-day episode currently becomes
one alert entity per day (issue #37). The planned fix merges contiguous
same-(department, phenomenon) warnings into one episode, which rests on three
assumptions a single feed fetch cannot test:

* **A1** a skipped forecast day means a genuinely separate episode (never
  observed; needs multi-day sampling),
* **A2** at most one live warning exists per (department, phenomenon, day),
* **A3** one in-effect and one pending warning coexist for the same cell for
  part of each day.

Status as of 2026-07-30: **A3 confirmed** (19/24 samples), and notably confirmed
with no samples in the 16:00-24:00 Paris window it was predicted for — the
overlap is a today+tomorrow day pair live all morning, not an afternoon-only
effect. A1 remains untested. A2 was *unmeasurable* by the first version of this
script: it keyed cells on (department, awareness_type) with no day component and
stored forecast days in a set, so two warnings sharing one (cell, day) collapsed
to a single entry. The ``*_cell_day`` counters added 2026-07-30 measure A2
properly; samples logged before then lack those keys and cannot answer it.

``--sample`` appends one JSON line of per-cell statistics and stores the raw
payload gzipped. ``--report`` reads the log back and prints a verdict per
assumption. Intended to run every 30 minutes for several days.

Standalone dev tooling: stdlib only, no repo imports.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FEED_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-france"
MF_SENDER = "vigilance@meteo.fr"
PARIS = ZoneInfo("Europe/Paris")
DEFAULT_DIR = Path.home() / "meteoalarm-fr-sample"
RAW_RETENTION_DAYS = 14
FETCH_TIMEOUT_S = 30
# The window A3's overlap was originally predicted for, on the theory that
# MeteoFrance publishes the next day's bulletin in the afternoon. Live data
# disproved the theory (the overlap runs all morning), so this is retained only
# as a coverage statistic — nothing keys a verdict on it.
OVERLAP_HOURS = range(16, 24)


def fetch(url: str = FEED_URL) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cap-alerts-probe/1.0"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read()


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fr_info(alert: dict[str, Any]) -> dict[str, Any] | None:
    infos = alert.get("info") or []
    for info in infos:
        if str(info.get("language") or "").lower().startswith("fr"):
            return info
    return infos[0] if infos else None


def awareness_code(info: dict[str, Any]) -> str:
    """Leading token of ``awareness_type`` ("5; high-temperature" -> "5")."""
    for param in info.get("parameter") or []:
        if param.get("valueName") == "awareness_type":
            return str(param.get("value") or "").split(";", 1)[0].strip()
    return ""


def nuts3_codes(info: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for area in info.get("area") or []:
        for code in area.get("geocode") or []:
            if code.get("valueName") == "NUTS3":
                value = str(code.get("value") or "")
                if value and value not in out:
                    out.append(value)
    return out


def classify(onset: datetime | None, expires: datetime | None, now: datetime) -> str:
    """Lifecycle bucket for one warning.

    MeteoFrance encodes "no warning / green" as an ``Update`` with a degenerate
    window, in two flavours seen live on 2026-07-30:

    * ``superseded`` — ``expires < onset``: its replacement marker, rewriting
      ``expires`` to the replacing message's issue time.
    * ``degenerate`` — ``expires == onset``: a zero-length window. All 19 in that
      sample were ``msgType=Update`` at ``awareness_level=1`` (green). Bucketed
      separately because a strict ``expires < onset`` test misses them and they
      then masquerade as live *pending* warnings, which is what made the first
      version of this script report A2 as violated 73 times over.

    Neither is a live warning.
    """
    if onset is not None and expires is not None and expires < onset:
        return "superseded"
    if onset is not None and expires is not None and expires == onset:
        return "degenerate"
    if expires is not None and expires <= now:
        return "finished"
    if onset is not None and onset > now:
        return "pending"
    return "in_effect"


def day_key(info: dict[str, Any], alert: dict[str, Any]) -> str:
    for value in (info.get("onset"), info.get("effective"), alert.get("sent")):
        if value:
            return str(value)[:10]
    return ""


def has_day_gap(days: set[str]) -> bool:
    """True when the cell's live forecast days are not a contiguous run."""
    parsed = sorted(date.fromisoformat(d) for d in days if d)
    if len(parsed) < 2:
        return False
    return any(
        (parsed[i + 1] - parsed[i]) > timedelta(days=1) for i in range(len(parsed) - 1)
    )


def analyze(payload: dict[str, Any], now: datetime, size: int) -> dict[str, Any]:
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        raise ValueError("feed missing 'warnings' array")

    states: dict[str, int] = defaultdict(int)
    total = 0
    mf_total = 0
    # cell -> state -> set of forecast days. Feeds A1 and A3, which are questions
    # about how many distinct *days* a cell has live. Cannot answer A2: the set
    # collapses two warnings that share a (cell, day).
    cells: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    # (cell, day) -> number of warnings. This is what A2 actually asks about.
    cell_day_live: dict[tuple[str, str, str], int] = defaultdict(int)
    cell_day_in_effect: dict[tuple[str, str, str], int] = defaultdict(int)

    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        alert = warning.get("alert") or {}
        total += 1
        if (alert.get("sender") or "") != MF_SENDER:
            continue
        if (alert.get("status") or "Actual") != "Actual":
            continue
        info = fr_info(alert)
        if info is None:
            continue
        mf_total += 1
        state = classify(
            parse_iso(str(info.get("onset") or "")),
            parse_iso(str(info.get("expires") or "")),
            now,
        )
        states[state] += 1
        if state not in ("in_effect", "pending"):
            continue
        code = awareness_code(info)
        day = day_key(info, alert)
        for region in nuts3_codes(info):
            cells[(region, code)][state].add(day)
            cell_day_live[(region, code, day)] += 1
            if state == "in_effect":
                cell_day_in_effect[(region, code, day)] += 1

    gt1_live: list[str] = []
    gt1_in_effect: list[str] = []
    both: list[str] = []
    gapped: list[str] = []
    max_live = 0
    max_in_effect = 0

    for (region, code), by_state in cells.items():
        label = f"{region}/{code}"
        in_effect_days = by_state.get("in_effect", set())
        pending_days = by_state.get("pending", set())
        live_days = in_effect_days | pending_days
        max_live = max(max_live, len(live_days))
        max_in_effect = max(max_in_effect, len(in_effect_days))
        if len(live_days) > 1:
            gt1_live.append(f"{label}:{sorted(live_days)}")
        if len(in_effect_days) > 1:
            gt1_in_effect.append(f"{label}:{sorted(in_effect_days)}")
        if in_effect_days and pending_days:
            both.append(f"{label}:{sorted(in_effect_days)}+{sorted(pending_days)}")
        if has_day_gap(live_days):
            gapped.append(f"{label}:{sorted(live_days)}")

    # A2: a cell-day carrying more than one warning is a real same-day duplicate.
    gt1_same_day = [
        f"{region}/{code}@{day}:{count}"
        for (region, code, day), count in cell_day_live.items()
        if count > 1
    ]
    gt1_same_day_in_effect = [
        f"{region}/{code}@{day}:{count}"
        for (region, code, day), count in cell_day_in_effect.items()
        if count > 1
    ]

    all_days = {d for by_state in cells.values() for s in by_state.values() for d in s}
    return {
        "ts_utc": now.isoformat(),
        "ts_paris": now.astimezone(PARIS).isoformat(),
        "paris_hour": now.astimezone(PARIS).hour,
        "bytes": size,
        "warnings_total": total,
        "warnings_mf": mf_total,
        "states": dict(states),
        "cells_live": len(cells),
        # These count distinct live *days* per cell (A1/A3), not warnings.
        "max_live_per_cell": max_live,
        "max_in_effect_per_cell": max_in_effect,
        "cells_gt1_live": len(gt1_live),
        "cells_gt1_in_effect": len(gt1_in_effect),
        "cells_with_both": len(both),
        "cells_with_gap": len(gapped),
        # These count warnings per (cell, day) — the A2 question. Absent from
        # samples logged before 2026-07-30.
        "cell_days_live": len(cell_day_live),
        "max_live_per_cell_day": max(cell_day_live.values(), default=0),
        "max_in_effect_per_cell_day": max(cell_day_in_effect.values(), default=0),
        "cell_days_gt1_live": len(gt1_same_day),
        "cell_days_gt1_in_effect": len(gt1_same_day_in_effect),
        "day_min": min(all_days) if all_days else "",
        "day_max": max(all_days) if all_days else "",
        "examples": {
            "gt1_live": sorted(gt1_live)[:5],
            "gt1_in_effect": sorted(gt1_in_effect)[:5],
            "both": sorted(both)[:5],
            "gap": sorted(gapped)[:5],
            "gt1_same_day": sorted(gt1_same_day)[:5],
            "gt1_same_day_in_effect": sorted(gt1_same_day_in_effect)[:5],
        },
    }


def prune_raw(raw_dir: Path, now: datetime) -> None:
    cutoff = now.timestamp() - RAW_RETENTION_DAYS * 86400
    for path in raw_dir.glob("*.json.gz"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def do_sample(base: Path) -> int:
    raw_dir = base / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    log = base / "samples.jsonl"
    errors = base / "errors.log"
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    try:
        body = fetch()
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        with errors.open("a", encoding="utf-8") as fh:
            fh.write(f"{now.isoformat()} fetch failed: {err!r}\n")
        return 1

    try:
        payload = json.loads(body)
        record = analyze(payload, now, len(body))
    except (ValueError, TypeError) as err:
        with errors.open("a", encoding="utf-8") as fh:
            fh.write(f"{now.isoformat()} parse failed: {err!r}\n")
        return 1

    with gzip.open(raw_dir / f"{stamp}.json.gz", "wb") as gz:
        gz.write(body)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    prune_raw(raw_dir, now)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def load_samples(base: Path) -> list[dict[str, Any]]:
    log = base / "samples.jsonl"
    if not log.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def do_report(base: Path) -> int:
    samples = load_samples(base)
    if not samples:
        print(f"no samples yet in {base}/samples.jsonl")
        return 1

    hours = {s.get("paris_hour") for s in samples}
    overlap_samples = [s for s in samples if s.get("paris_hour") in OVERLAP_HOURS]
    days_covered = sorted({str(s.get("ts_paris", ""))[:10] for s in samples})

    print(f"samples:        {len(samples)}")
    print(f"first:          {samples[0].get('ts_paris')}")
    print(f"last:           {samples[-1].get('ts_paris')}")
    print(
        f"paris days:     {len(days_covered)} ({days_covered[0]}..{days_covered[-1]})"
    )
    print(f"paris hours hit: {len(hours)}/24  sorted={sorted(h for h in hours)}")
    print(f"samples in the 16:00-24:00 overlap window: {len(overlap_samples)}")
    print(f"latest sample states: {samples[-1].get('states', {})}")
    print(
        "  (superseded/degenerate are MeteoFrance's green 'no warning' markers "
        "and are excluded from every verdict below)"
    )
    print()

    # Only samples carrying the cell-day counters can speak to A2 at all.
    measured = [s for s in samples if "max_live_per_cell_day" in s]
    unmeasured = len(samples) - len(measured)
    violating = [s for s in measured if s.get("cell_days_gt1_live", 0)]
    print("A2  at most one live warning per (department, phenomenon, day)")
    print(f"    samples able to answer this: {len(measured)}/{len(samples)}")
    if unmeasured:
        print(
            f"    ({unmeasured} earlier sample(s) predate the cell-day counters "
            "and are blind to same-day duplicates)"
        )
    if not measured:
        print("    UNMEASURED: no sample carries per-(cell, day) warning counts yet")
    elif violating:
        max_per_cell_day = max(s.get("max_live_per_cell_day", 0) for s in measured)
        print(f"    VIOLATED in {len(violating)}/{len(measured)} sample(s)")
        print(f"    max live warnings in one cell-day: {max_per_cell_day}")
        for s in violating[:3]:
            print(f"      {s.get('ts_paris')} {s['examples'].get('gt1_same_day')}")
        print("    => the merge's same-day tie-break is a REAL path, not defensive")
    else:
        print("    holds: no cell-day carried more than one live warning")
    print()

    with_both = [s for s in samples if s.get("cells_with_both", 0)]
    overlap_with_both = [s for s in overlap_samples if s.get("cells_with_both", 0)]
    max_days = max(s.get("max_live_per_cell", 0) for s in samples)
    max_in_effect_days = max(s.get("max_in_effect_per_cell", 0) for s in samples)
    print("A3  in-effect and pending coexist for the same cell part of each day")
    print(f"    samples showing the overlap: {len(with_both)}/{len(samples)}")
    print(f"    max distinct live days in one cell:      {max_days}")
    print(f"    max distinct IN-EFFECT days in one cell: {max_in_effect_days}")
    print(
        "    within the predicted 16:00-24:00 window (informational only): "
        f"{len(overlap_with_both)}/{len(overlap_samples)}"
    )
    if with_both:
        peak = max(with_both, key=lambda s: s.get("cells_with_both", 0))
        print(
            f"    peak: {peak.get('cells_with_both')} cells at {peak.get('ts_paris')}"
        )
        print(f"      e.g. {peak['examples'].get('both')}")
        print("    CONFIRMED")
    elif not overlap_samples:
        print("    UNTESTED: no samples yet inside the overlap window")
    else:
        print("    NOT OBSERVED so far — investigate before merging the episode PR")
    print()

    gapped = [s for s in samples if s.get("cells_with_gap", 0)]
    print("A1  a skipped forecast day means a separate episode")
    print(
        f"    samples with a non-contiguous live day set: {len(gapped)}/{len(samples)}"
    )
    if gapped:
        print("    gap cases observed (each is a real two-episode case to model):")
        for s in gapped[:5]:
            print(f"      {s.get('ts_paris')} {s['examples'].get('gap')}")
    else:
        print("    no gap case seen yet; contiguity rule untested against live data")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="summarize collected samples instead of fetching a new one",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help=f"state directory (default: {DEFAULT_DIR})",
    )
    args = parser.parse_args(argv)
    args.dir.mkdir(parents=True, exist_ok=True)
    if args.report:
        return do_report(args.dir)
    return do_sample(args.dir)


if __name__ == "__main__":
    # Stagger slightly so a cron fleet doesn't hit the feed on the exact minute.
    if "--report" not in sys.argv:
        time.sleep(min(5.0, abs(hash(time.time())) % 5))
    sys.exit(main())
