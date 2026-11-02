#!/usr/bin/env python3
"""Read-only probe: which live feed and CAP bodies would aiohttp fail to decode?

``resp.text()`` decodes strictly: with the charset from ``Content-Type`` when
there is one, otherwise UTF-8, ignoring an XML declaration (issue #371). This
walks every authority feed in the WMO registry, every SWIC mirror feed that
still answers, the four AU state feeds and both ECCC hosts, follows up to
``--bodies`` CAP links per feed, and reports each body whose bytes would raise
``UnicodeDecodeError`` that way, with what its header and its XML declaration
say. Exits 1 if any does. Stdlib only, identifies itself, writes nothing
upstream.

The mirror is found, not listed: every registry id and bucket folder name is
tried and whatever answers 200 is walked, since ``auto`` still reads it where
it answers and the set shrinks without notice (two sources in October 2026,
one by November).

    scripts/charset_probe.py [--bodies N] [--workers N] [--limit N] [--no-mirror]
"""

from __future__ import annotations

import argparse
import codecs
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET

REGISTRY = "https://severeweather.wmo.int/v2/json/sources.json"
MIRROR = "https://severeweather.wmo.int/v2/cap-alerts/{source_id}/rss.xml"
BUCKET_FOLDER = re.compile(r"cap-sources\.s3\.amazonaws\.com/([^/]+)/rss\.xml$")
FIXED_FEEDS = {
    "au-nsw": "https://www.rfs.nsw.gov.au/feeds/majorIncidentsCAP.xml",
    "au-qld": (
        "https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com"
        "/content/Feeds/BushfireCurrentIncidents/bushfireAlert_capau.xml"
    ),
    "au-wa": "https://api.emergency.wa.gov.au/v1/capau",
    "au-tas": "https://alert.tas.gov.au/data/cap-au.xml",
    "eccc-alertready": "https://rss.alertready.ca/",
    "eccc-pelmorex": "https://rss.naad-adna.pelmorex.com/",
}
UA = "cap_alerts-charset-probe/1 (read-only; github.com/seevee/cap_alerts)"
XML_DECL = re.compile(rb"""^\s*<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


def fetch(url: str, timeout: float = 30) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, "", b""
    except (OSError, ValueError):  # URLError, timeouts, TLS, a malformed link
        return 0, "", b""


def header_charset(content_type: str) -> str | None:
    m = re.search(r"charset\s*=\s*\"?([A-Za-z0-9._-]+)", content_type, re.IGNORECASE)
    if not m:
        return None
    try:
        return codecs.lookup(m.group(1)).name
    except LookupError:
        return None  # aiohttp falls back to the resolver, which says utf-8


def aiohttp_verdict(content_type: str, body: bytes) -> tuple[bool, str]:
    """(decodes, the codec aiohttp would use) for ``resp.text()``."""
    codec = header_charset(content_type) or "utf-8"
    try:
        body.decode(codec)
    except UnicodeDecodeError:
        return False, codec
    return True, codec


def xml_declared(body: bytes) -> str:
    m = XML_DECL.match(body[:200])
    return m.group(1).decode("ascii").lower() if m else ""


def cap_links(body: bytes) -> list[str]:
    """CAP document links from an RSS or Atom index; anything unparseable gives none."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    links: list[str] = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag == "item":
            for child in item:
                if (
                    child.tag.rsplit("}", 1)[-1] == "link"
                    and (child.text or "").strip()
                ):
                    links.append(child.text.strip())
        elif tag == "entry":
            hrefs = [
                c.get("href", "")
                for c in item
                if c.tag.rsplit("}", 1)[-1] == "link" and c.get("href")
            ]
            cap = [h for h in hrefs if "cap" in h.lower() or h.endswith(".xml")]
            links.extend(cap[:1] or hrefs[:1])
    return links


def check(label: str, url: str) -> dict:
    status, ctype, body = fetch(url)
    ok, codec = aiohttp_verdict(ctype, body) if status == 200 else (True, "")
    return {
        "label": label,
        "url": url,
        "status": status,
        "content_type": ctype,
        "declared": xml_declared(body) if status == 200 else "",
        "decodes": ok,
        "codec": codec,
        "body": body if status == 200 else b"",
    }


def registry_feeds(limit: int | None) -> tuple[dict[str, str], set[str]]:
    """Authority feeds by label, and every id the mirror could serve.

    Mirror ids are the registry's ``sourceId``s plus the bucket folder names,
    which name one language feed each (``tl-dnmg-pt`` under ``tl-dnmg-en``).
    """
    _status, _ctype, raw = fetch(REGISTRY)
    feeds: dict[str, str] = {}
    ids: set[str] = set()
    for entry in json.loads(raw)["sources"]:
        src = entry["source"]
        ids.add(src["sourceId"])
        for i, url in enumerate(str(src.get("capAlertFeed") or "").split(",")):
            if url.strip():
                feeds[f"wmo:{src['sourceId']}#{i}"] = url.strip()
                if m := BUCKET_FOLDER.search(url.strip()):
                    ids.add(m.group(1))
    items = sorted(feeds.items())
    return dict(items[:limit] if limit else items), ids


def live_mirror_feeds(ids: set[str], workers: int) -> dict[str, str]:
    """The mirror feeds that still answer 200, by label."""
    urls = {f"mirror:{sid}": MIRROR.format(source_id=sid) for sid in sorted(ids)}
    with ThreadPoolExecutor(workers) as pool:
        status = dict(zip(urls, pool.map(lambda u: fetch(u)[0], urls.values())))
    return {label: url for label, url in urls.items() if status[label] == 200}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bodies", type=int, default=5, help="CAP links followed per feed")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, help="first N WMO feeds only")
    ap.add_argument(
        "--no-mirror", action="store_true", help="skip the SWIC mirror sweep"
    )
    args = ap.parse_args()

    authority, ids = registry_feeds(args.limit)
    mirror = {} if args.no_mirror else live_mirror_feeds(ids, args.workers)
    print(f"mirror feeds answering: {sorted(mirror) or 'none'}")
    feeds = {**FIXED_FEEDS, **authority, **mirror}
    with ThreadPoolExecutor(args.workers) as pool:
        indexes = list(pool.map(lambda kv: check(*kv), feeds.items()))
        jobs = [
            (f"{ix['label']} body", link)
            for ix in indexes
            for link in cap_links(ix["body"])[: args.bodies]
        ]
        bodies = list(pool.map(lambda kv: check(*kv), jobs))

    tally: Counter[str] = Counter()
    for kind, rows in (("index", indexes), ("body", bodies)):
        for r in rows:
            if r["status"] != 200:
                tally[
                    f"{kind} {'unreachable' if r['status'] == 0 else 'http ' + str(r['status'])}"
                ] += 1
            elif r["decodes"]:
                tally[f"{kind} decodes"] += 1
            else:
                tally[f"{kind} FAILS"] += 1
                print(
                    f"FAIL {r['label']}: {r['codec']} per header {r['content_type']!r}, "
                    f"declared {r['declared'] or '-'}  {r['url']}"
                )
    declared = Counter(
        r["declared"] or "-" for r in indexes + bodies if r["status"] == 200
    )
    no_charset = sum(
        1
        for r in indexes + bodies
        if r["status"] == 200 and not header_charset(r["content_type"])
    )
    print(f"\nfeeds {len(indexes)}, bodies {len(bodies)}: {dict(tally)}")
    print(f"XML-declared encodings: {dict(declared)}")
    print(f"served with no charset in Content-Type: {no_charset}")
    return (
        1
        if any(not r["decodes"] for r in indexes + bodies if r["status"] == 200)
        else 0
    )


if __name__ == "__main__":
    sys.exit(main())
