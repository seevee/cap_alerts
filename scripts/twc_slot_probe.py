#!/usr/bin/env python3
"""Report what the U.S. Tsunami Warning Centers' product slots hold (issue #298).

tsunami.gov publishes no feed. Each product (``{TWCID}{WMOID}``, e.g.
``PHEBWEHW40`` for PTWC's Hawaii warnings) has one JSON slot holding its latest
bulletin, and the slot's ``eventDir`` plus ``{TWCID}CAP.xml`` is that
bulletin's CAP. This walks every known slot and prints, per product: the
bulletin it holds, the CAP message family it carries, and whether it is still
live. Then it checks the two cross-slot facts the provider design rests on:

* ``<identifier>`` collisions: PTWC writes ``PHEB-{n}-{eventid}`` for every
  product of one event, so two slots can hold different messages under one
  identifier.
* conditional GET: a slot answers ``If-Modified-Since`` with ``304``.

Slots are not enumerable (a product's slot 404s until it is first issued),
hence ``--discover``, which brute-forces the ``WE{region}{nn}`` space.

Stdlib only, read-only:

    python3 scripts/twc_slot_probe.py
    python3 scripts/twc_slot_probe.py --discover
"""

from __future__ import annotations

import argparse
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone

BASE = "https://www.tsunami.gov"
USER_AGENT = (
    "cap-alerts-twc-slot-probe/1.0 (+https://github.com/seevee/cap_alerts; "
    "read-only, issue 298)"
)
NS = {"c": "urn:oasis:names:tc:emergency:cap:1.2"}

# Every slot that answered 200 on 2026-10-27. XX30 (NTWC U.S. East/Gulf
# warnings) is assumed by symmetry with XX32 and has never been issued.
KNOWN_SLOTS = (
    "PAAQWEAK51",  # NTWC warnings: AK, BC, U.S. West Coast
    "PAAQWEAK53",  # NTWC information statements, same area
    "PAAQWEXX32",  # NTWC information statements: U.S. East/Gulf coast
    "PHEBWEHW40",  # PTWC warnings: Hawaii
    "PHEBWEHW42",  # PTWC information statements: Hawaii
    "PHEBWEGM40",  # PTWC warnings: Guam / CNMI
    "PHEBWEGM42",
    "PHEBWEZS40",  # PTWC warnings: American Samoa
    "PHEBWEZS42",
    "PHEBWECA40",  # PTWC warnings: Puerto Rico / USVI
    "PHEBWECA42",
    "PHEBWECA41",  # PTWC international threat messages: Caribbean
    "PHEBWECA43",  # PTWC international information: Caribbean
    "PHEBWEPA40",  # PTWC international threat messages: Pacific
    "PHEBWEPA42",  # PTWC international information: Pacific
)

# Some slots are not valid JSON (``"eventLat" : 28.,`` on PHEBWEHW42), so the
# one field needed is read with a pattern rather than ``json.loads``.
_EVENT_DIR_RE = re.compile(r'"eventDir"\s*:\s*"([^"]+)"')


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as exc:
        return exc.code, b"", ""


def _summarize(slot: str, xml: bytes) -> dict[str, str]:
    root = ET.fromstring(xml)

    def text(el: ET.Element, tag: str) -> str:
        return (el.findtext(f"c:{tag}", "", NS) or "").strip()

    infos = root.findall("c:info", NS)
    first = infos[0] if infos else None
    params = {}
    if first is not None:
        for p in first.findall("c:parameter", NS):
            params[text(p, "valueName")] = text(p, "value")
    ugc = (
        [
            text(g, "value")
            for a in first.findall("c:area", NS)
            for g in a.findall("c:geocode", NS)
        ]
        if first is not None
        else []
    )
    expires = text(first, "expires") if first is not None else ""
    live = "no info (final)" if first is None else "no expires"
    if expires:
        try:
            at = datetime.fromisoformat(expires.replace("-00:00", "+00:00"))
            live = "live" if at > datetime.now(timezone.utc) else "expired"
        except ValueError:
            live = f"bad expires {expires!r}"
    return {
        "slot": slot,
        "identifier": text(root, "identifier"),
        "sent": text(root, "sent"),
        "msgType": text(root, "msgType"),
        "incidents": text(root, "incidents"),
        "refs": str(len(text(root, "references").split())),
        "infos": str(len(infos)),
        "event": text(first, "event") if first is not None else "",
        "severity": text(first, "severity") if first is not None else "",
        "urgency": text(first, "urgency") if first is not None else "",
        "ugc": str(len(ugc)),
        "vtec": params.get("VTEC", ""),
        "state": live,
    }


def discover() -> None:
    for twc in ("PAAQ", "PHEB"):
        for region in ("AK", "CA", "GM", "HW", "PA", "XX", "ZS", "IO", "AT"):
            for nn in range(20, 62):
                slot = f"{twc}WE{region}{nn}"
                status, _, _ = _get(f"{BASE}/events/json/{slot}.json")
                if status == 200:
                    print(slot)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--discover", action="store_true")
    args = parser.parse_args()
    if args.discover:
        discover()
        return

    rows = []
    by_identifier: dict[str, list[str]] = defaultdict(list)
    validators: dict[str, str] = {}
    for slot in KNOWN_SLOTS:
        status, body, last_modified = _get(f"{BASE}/events/json/{slot}.json")
        if status != 200:
            print(f"{slot}: slot HTTP {status}")
            continue
        validators[slot] = last_modified
        match = _EVENT_DIR_RE.search(body.decode("utf-8", "replace"))
        if not match:
            print(f"{slot}: no eventDir")
            continue
        cap_url = f"{BASE}{match.group(1)}{slot[:4]}CAP.xml"
        status, xml, _ = _get(cap_url)
        if status != 200:
            print(f"{slot}: CAP HTTP {status} {cap_url}")
            continue
        row = _summarize(slot, xml)
        rows.append(row)
        by_identifier[row["identifier"]].append(slot)

    cols = ("slot", "identifier", "sent", "msgType", "infos", "event", "severity")
    cols += ("urgency", "ugc", "state", "vtec")
    print("\t".join(cols))
    for row in rows:
        print("\t".join(row[c] for c in cols))

    print("\nidentifier collisions across slots:")
    collisions = {k: v for k, v in by_identifier.items() if len(v) > 1}
    for identifier, slots in collisions.items():
        print(f"  {identifier}: {', '.join(slots)}")
    if not collisions:
        print("  none")

    slot, last_modified = next(iter(validators.items()), ("", ""))
    if last_modified:
        status, _, _ = _get(
            f"{BASE}/events/json/{slot}.json",
            {"If-Modified-Since": last_modified},
        )
        print(f"\nconditional GET on {slot}: HTTP {status}")


if __name__ == "__main__":
    main()
