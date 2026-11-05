#!/usr/bin/env python3
"""Per-entry startup timing from a Home Assistant log (issue #358).

Reads one boot's ``home-assistant.log`` and prints, for every cap_alerts entry,
when its first successful fetch finished (seconds after core's ``Setting up
cap_alerts`` line), how long that fetch took, and how many attempts it needed.
A tracker entry fails its first attempt in 0.000 s while the device tracker has
no location yet and is ready only after the retry, which is why the attempts
column matters.

Needs the coordinator's debug line and core's setup line, so the boot has to
run with this in ``configuration.yaml``:

    logger:
      logs:
        homeassistant.setup: info
        custom_components.cap_alerts.coordinator: debug

Rows are keyed by config entry id; ``integration/setup_info`` over the
WebSocket gives the config-entry phase's wall clock to read beside them.
Stdlib only, reads the file, writes nothing.

    scripts/startup_sample.py <home-assistant.log>
"""

from __future__ import annotations

import re
import statistics
import sys
from datetime import datetime, timezone

FETCH = re.compile(
    r"^(\S+ \S+) .*Finished fetching cap_alerts_(\w+) data in ([\d.]+) seconds"
    r" \(success: (True|False)\)"
)
SETUP = re.compile(r"^(\S+ \S+) .*\[homeassistant\.setup\] Setting up cap_alerts$")


def stamp(text: str) -> datetime:
    # Log times are local and only ever subtracted from each other, so the
    # zone cancels; UTC just makes the value aware.
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.split("\n\n")[-1].strip(), file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8", errors="replace") as log:
        lines = log.read().splitlines()
    start = next(
        (stamp(m.group(1)) for line in lines if (m := SETUP.match(line))), None
    )
    if start is None:
        print("no 'Setting up cap_alerts' line: is homeassistant.setup at info?")
        return 1

    entries: dict[str, dict[str, float]] = {}
    for line in lines:
        m = FETCH.match(line)
        if not m:
            continue
        rec = entries.setdefault(m.group(2), {"attempts": 0})
        if "ready" in rec:
            continue  # later polls, not the boot
        rec["attempts"] += 1
        if m.group(4) == "True":
            rec["fetch"] = float(m.group(3))
            rec["ready"] = (stamp(m.group(1)) - start).total_seconds()

    if not entries:
        print("no fetch lines: is custom_components.cap_alerts.coordinator at debug?")
        return 1
    for entry_id, rec in sorted(
        entries.items(), key=lambda kv: kv[1].get("ready", 1e9)
    ):
        ready = f"{rec['ready']:6.1f}s" if "ready" in rec else "  never"
        fetch = f"{rec['fetch']:5.2f}s" if "fetch" in rec else "    -"
        print(
            f"{ready} ready | fetch {fetch} | attempts {int(rec['attempts'])} | {entry_id}"
        )
    fetches = sorted(rec["fetch"] for rec in entries.values() if "fetch" in rec)
    print(
        f"\n{len(entries)} entries, median first fetch {statistics.median(fetches):.2f}s, "
        f"slowest {fetches[-1]:.2f}s, last ready "
        f"{max(rec.get('ready', 0.0) for rec in entries.values()):.1f}s after setup began"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
