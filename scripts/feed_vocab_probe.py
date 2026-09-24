#!/usr/bin/env python3
"""Detect provider feed vocabulary drift before a user report does (issue #172).

ECCC launched Convective Alert Modernization on 2026-08-11: severe
thunderstorm and tornado warnings grew a freeform threat-area ``<area>``
marked by a new ``layer:EC-MSC-SMC:DLC:1.1`` geocode, and GPS matching was
wrong about it for twelve days until a user noticed. The change was visible
in the feed itself from day one — as vocabulary this integration had never
seen. This probe exists so the *next* unannounced change is a scheduled-run
diff instead of a bug report.

It fetches each provider's live feed, extracts its structural vocabulary —
element/key paths, geocode schemes, parameter keys, and the small enumerable
value sets (status, severity, lifecycle tokens, the DLC values) — and diffs
the result against ``scripts/feed_vocab_baseline.json``. New tokens are
drift; tokens absent from a given run are not (a quiet week removes nothing),
so the baseline only ever grows and its diffs stay reviewable.

**Every run that finds drift opens or updates a GitHub issue**, so the bar
for what counts is deliberately high. A value set is tracked only when its
full membership is known *up front* — a spec enumeration (CAP 1.2), a
catalog endpoint (NWS ``/alerts/types``), or documented codes (GDACS event
types, MeteoAlarm awareness codes) — or when it is a lifecycle vocabulary
whose new token would change how alerts retire (ECCC ``Alert_Location_Status``,
the DLC values). A set that would merely fill in as weather happens is not
tracked at all: the first seeded baseline learned ``values.event`` from one
afternoon's live NWS feed, and the first scheduled run filed an issue because
a tornado warning had appeared (#175). Open sets — areaDesc, Alert_Name's
colour × event product, identifiers, member-service parameter names — are
out for the same reason. ECCC vocabulary is scoped to the cap-pac@canada.ca
sender: the NAAD channel carries provincial EMOs, Amber alerts and test
traffic whose comings and goings are not ECCC drift. NWS parameter keys
are read from NWS-originated alerts only: the active feed relays IPAWS
traffic from local originators, who name their parameters however they
like (a county emergency manager's ``timezone``, #184).

Every new token is reported with the ids of the first few alerts that
carried it, so a drift issue names documents to open rather than just a
token that may have aged out of the feed by the time anyone reads it. NWS
is also read over the last 48 h of alerts, not just the active instant,
since the API answers historical queries; the other feeds show only what
is live, and the daily schedule is what bounds the gap there.

WMO is probed for a different failure (issue #210): the SWIC mirror the
integration polls can silently stop following a source's own feed. Three
Timor-Leste alerts issued over 2024-2025 never reached the mirror, and a
user on ``tl-dnmg-en`` saw an empty feed, which looks exactly like no
warnings in force. Vocabulary is not sampled there (per-configured-source
RSS, no bounded national endpoint), but the ~120 sources whose registry
``capAlertFeed`` lives on the cap-sources S3 bucket can be listed, so the
probe compares each mirror's newest item with the newest ``Actual`` alert
its bucket received at least a week ago and reports a mirror that still
lacks it, provided the authority has published in the last 90 days: a stall
costs nothing while the source is dormant, and it files a week after the
source publishes again. The token carries the date the mirror stopped
(``tl-dnmg-en@2023-12-20``), so a mirror that recovers and stalls again is
new drift. On the day it was written 14 of 93 mirrored feeds were behind and
one (Egypt) had a live authority behind it.

Usage:
    scripts/feed_vocab_probe.py                    # probe all, diff vs baseline
    scripts/feed_vocab_probe.py --update           # fold new vocabulary in
    scripts/feed_vocab_probe.py --providers eccc,nws
    scripts/feed_vocab_probe.py --report-file drift.md   # issue-ready markdown

Exit status:
    0  no drift
    1  drift found (cron-friendly: non-zero means "look at the report")
    2  a provider could not be probed at all (drift, if any, still reported)

Run daily by .github/workflows/provider-drift.yml, which opens/updates a
``provider-drift`` issue from the report (skipping a comment that would only
repeat tokens already on the open issue). To accept drift, review it, then
run ``--update`` and PR the baseline change.

Stdlib only — run with system python3, no venv needed. (It loads
``custom_components/cap_alerts/const.py`` by path; that file is pure Python.)
"""

from __future__ import annotations

import argparse

# const.py is pure Python but sits behind a package __init__ that imports
# homeassistant, so it is loaded by file path under a private name. That
# pattern is banned in tests/ (import hygiene, #137) because a second copy
# shadows the real module for the rest of a pytest session; this is a
# standalone process that never sees the real one.
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

_const_spec = importlib.util.spec_from_file_location(
    "_cap_alerts_const",
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "cap_alerts"
    / "const.py",
)
assert _const_spec and _const_spec.loader
_const = importlib.util.module_from_spec(_const_spec)
_const_spec.loader.exec_module(_const)

GDACS_RSS_CURRENT_URL: str = _const.GDACS_RSS_CURRENT_URL
GDACS_RSS_24H_URL: str = _const.GDACS_RSS_24H_URL
BBK_MAPDATA_URL: str = _const.BBK_MAPDATA_URL
BBK_WARNING_URL: str = _const.BBK_WARNING_URL
BBK_CHANNELS: tuple[str, ...] = _const.BBK_CHANNELS
METEOALARM_COUNTRY_SLUGS: dict[str, str] = _const.METEOALARM_COUNTRY_SLUGS

# Mirrors of constants that live in provider modules this script cannot import
# without a venv (they pull in aiohttp/homeassistant). Authoritative homes:
# providers/eccc.py::NAAD_FEED_ALERTREADY, providers/nws.py::NWS_API_BASE,
# providers/meteoalarm.py::METEOALARM_FEED_URL.
NAAD_FEED_ALERTREADY = "https://rss.alertready.ca/"
NWS_ACTIVE_URL = "https://api.weather.gov/alerts/active"
# The active set is one instant; a 30-minute tornado warning is in it only if
# live at 13:43 UTC. Alerts sent in this window are read as well, so a product
# that came and went between runs is still sampled. 48 h so consecutive daily
# runs overlap, and to match the NAAD repository window.
NWS_HISTORY_URL = "https://api.weather.gov/alerts?start={start}&end={end}&limit=500"
NWS_LOOKBACK = timedelta(hours=48)
NWS_MAX_PAGES = 40  # 20k alerts; two days ran 2,771 in 7 pages on 2026-09-02
# The full NWS product catalog (111 names on 2026-08-23). This, not the live
# feed, is the ``values.event`` vocabulary: a product added or renamed here
# is drift (the 2025 Excessive → Extreme Heat rename is what an icon table
# needs to hear about); a known product going live is weather.
NWS_TYPES_URL = "https://api.weather.gov/alerts/types"
METEOALARM_FEED_URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{country}"
# providers/wmo.py::WMO_RSS_URL, the feed the integration actually polls.
WMO_MIRROR_RSS_URL = "https://severeweather.wmo.int/v2/cap-alerts/{source_id}/rss.xml"
WMO_SOURCES_URL: str = _const.WMO_SOURCES_URL
# Where the registry's ``capAlertFeed`` points for the authorities that
# publish through WMO's hosted CAP editor: one folder per language feed,
# one file per alert, publicly listable. The mirror is supposed to follow it.
CAP_SOURCES_BUCKET = "https://cap-sources.s3.amazonaws.com/"
# The grace the mirror gets to pick up a bucket file. A mirror still missing
# an Actual alert this long after it reached the bucket has stopped
# following it. Ingestion normally takes minutes (4 to 7 min across four
# sources measured on 2026-09-24) and the worst lag measured before a mirror
# stopped outright was 62 h; a week is comfortably past both, and short
# against the few alerts a month these authorities issue. Files younger than
# this are not judged at all: comparing against the bucket's newest file
# paged one hour after Senegal published its first alert in four months
# (#238), when the mirror had been given no chance to catch up.
WMO_MIRROR_LAG = timedelta(days=7)
# Bodies read back from the newest end of a lagging bucket to find its
# newest Actual alert (Test and Exercise traffic is not the mirror's job).
WMO_LAG_CONFIRM_BODIES = 5
# A lagging mirror is reported only while its authority is publishing: a
# source whose newest Actual alert is older than this is dormant, and a stall
# behind it costs no user anything until it wakes, at which point the same
# token files. Of the 14 mirrors behind on 2026-09-19, one passed this.
WMO_SOURCE_ACTIVE = timedelta(days=90)

BASELINE_PATH = Path(__file__).resolve().parent / "feed_vocab_baseline.json"

USER_AGENT = "cap-alerts-feed-vocab-probe/1.0 (+https://github.com/seevee/cap_alerts)"

PROVIDERS = ("eccc", "nws", "meteoalarm", "gdacs", "wmo", "bbk", "au")

# ECCC vocabulary is read from this sender only; see module docstring.
ECCC_SENDER = "cap-pac@canada.ca"
# NWS parameter keys likewise; the active feed also carries IPAWS relays.
NWS_SENDER = "w-nws.webmaster@noaa.gov"

# Alert ids printed per new token in the drift report.
MAX_WITNESS_IDS = 3

# The four Australian state feeds (issue #127), one EDXL-DE envelope each.
# Mirrors ``const.AU_FEEDS``; duplicated because this script is stdlib-only.
AU_FEEDS = {
    "NSW": "https://www.rfs.nsw.gov.au/feeds/majorIncidentsCAP.xml",
    "QLD": (
        "https://publiccontent-gis-psba-qld-gov-au.s3.amazonaws.com"
        "/content/Feeds/BushfireCurrentIncidents/bushfireAlert_capau.xml"
    ),
    "WA": "https://api.emergency.wa.gov.au/v1/capau",
    "TAS": "https://alert.tas.gov.au/data/cap-au.xml",
}
NS_EDXL = "urn:oasis:names:tc:emergency:EDXL:DE:1.0"

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_CAP = "urn:oasis:names:tc:emergency:cap:1.2"

# Namespace → path-prefix rendering for XML path extraction. CAP renders bare
# so its paths read like the spec ("alert/info/area/geocode"); an unmapped
# namespace renders in full, which makes a *new namespace* itself drift.
XML_PREFIXES = {
    NS_CAP: "",
    NS_ATOM: "atom:",
    "http://www.georss.org/georss": "georss:",
    "http://www.gdacs.org": "gdacs:",
    "http://purl.org/dc/elements/1.1/": "dc:",
    "http://www.w3.org/2003/01/geo/wgs84_pos#": "geo:",
    "http://www.w3.org/2000/09/xmldsig#": "ds:",
}

# CAP 1.2 closed enumerations (§3.2), pre-folded into the baseline on
# --update so a spec-legal value that merely wasn't live at seed time — a
# GDACS Red, an ECCC Extreme — never pages. Vocabulary outside the spec set
# still drifts normally.
_CAP_ENUMS = {
    "values.status": ["Actual", "Exercise", "System", "Test", "Draft"],
    "values.msgType": ["Alert", "Update", "Cancel", "Ack", "Error"],
    "values.scope": ["Public", "Restricted", "Private"],
    "values.category": [
        "Geo",
        "Met",
        "Safety",
        "Security",
        "Rescue",
        "Fire",
        "Health",
        "Env",
        "Transport",
        "Infra",
        "CBRNE",
        "Other",
    ],
    "values.responseType": [
        "Shelter",
        "Evacuate",
        "Prepare",
        "Execute",
        "Avoid",
        "Monitor",
        "Assess",
        "AllClear",
        "None",
    ],
    "values.urgency": ["Immediate", "Expected", "Future", "Past", "Unknown"],
    "values.severity": ["Extreme", "Severe", "Moderate", "Minor", "Unknown"],
    "values.certainty": ["Observed", "Likely", "Possible", "Unlikely", "Unknown"],
}

# NWS CAP ``parameters`` keys, per the api.weather.gov alert schema and the
# products observed across a summer and a winter feed. Seeded so a seasonal
# key (snow squalls) arriving in December is not "drift".
_NWS_PARAMETER_KEYS = [
    "AWIPSidentifier",
    "BLOCKCHANNEL",
    "CMAMlongtext",
    "CMAMtext",
    "EAS-ORG",
    "NWSheadline",
    "PIL",
    "VTEC",
    "WEAHandling",
    "WMOidentifier",
    "eventEndingTime",
    "eventMotionDescription",
    "expiredReferences",
    "flashFloodDamageThreat",
    "flashFloodDetection",
    "hailThreat",
    "maxHailSize",
    "maxWindGust",
    "snowSquallDetection",
    "snowSquallImpact",
    "thunderstormDamageThreat",
    "tornadoDamageThreat",
    "tornadoDetection",
    "waterspoutDetection",
    "windThreat",
]

# MeteoAlarm CAP Profile v2.0 §2.2.17: awareness_type codes 1–13 and
# awareness_level codes 1–4. Tracked as the bare code, because the label
# half of ``"10; Rain"`` is spelled per member service and already varies in
# case on the live hub.
_METEOALARM_AWARENESS_TYPE_CODES = [str(n) for n in range(1, 14)]
_METEOALARM_AWARENESS_LEVEL_CODES = [str(n) for n in range(1, 5)]

SPEC_VOCAB: dict[str, dict[str, list[str]]] = {
    "eccc": {
        **_CAP_ENUMS,
        "values.language": ["en-CA", "fr-CA"],
        "values.Alert_Type": ["advisory", "statement", "warning", "watch"],
        # Both languages, since the parameter is per-<info>.
        "values.Colour": ["yellow", "orange", "red", "jaune", "rouge"],
    },
    "meteoalarm": {
        **_CAP_ENUMS,
        "values.awareness_type": _METEOALARM_AWARENESS_TYPE_CODES,
        "values.awareness_level": _METEOALARM_AWARENESS_LEVEL_CODES,
    },
    "bbk": {
        **_CAP_ENUMS,
        # The DWD channel's hazard groups, as icons.py maps them (issue #66).
        "values.GROUP": [
            "WIND",
            "TORNADO",
            "THUNDERSTORM",
            "RAIN",
            "HAIL",
            "SNOWFALL",
            "ICE",
            "GLAZE",
            "FROST",
            "THAW",
            "FOG",
            "HEAT",
            "UV",
        ],
        # Every id prefix the API is known to mint; a new one is a new channel.
        "values.id_prefix": ["dwd", "dwdmap", "mow", "kat", "biw", "lhp"],
        # The channel index repeats three CAP enums per row (#217: a MoWaS
        # all-clear is a ``Cancel`` row, and stays listed for six hours).
        "index.severity": _CAP_ENUMS["values.severity"],
        "index.urgency": _CAP_ENUMS["values.urgency"],
        "index.type": _CAP_ENUMS["values.msgType"],
    },
    # NWS publishes the same sets under its GeoJSON property names.
    "nws": {
        **{
            bucket: tokens
            for bucket, tokens in _CAP_ENUMS.items()
            if bucket not in ("values.msgType", "values.responseType", "values.scope")
        },
        "values.messageType": _CAP_ENUMS["values.msgType"],
        "values.response": _CAP_ENUMS["values.responseType"],
        "parameter_keys": _NWS_PARAMETER_KEYS,
    },
    "au": {
        **_CAP_ENUMS,
        # The Australian Warning System ladder plus the informational tiers the
        # agencies publish below it; conventions.py maps each to a severity.
        "values.AlertLevel": [
            "Advice",
            "Watch and Act",
            "Emergency Warning",
            "Information",
            "Not Applicable",
            "Planned Burn",
            # TAS, on a smoke alert with no warning tier of its own (#217).
            "Not Yet Known",
        ],
    },
    "gdacs": {
        "values.alertlevel": ["Green", "Orange", "Red"],
        "values.episodealertlevel": ["Green", "Orange", "Red"],
        # The seven hazard types GDACS publishes.
        "values.eventtype": ["DR", "EQ", "FL", "TC", "TS", "VO", "WF"],
        "values.iscurrent": ["true", "false"],
    },
}

# Same OID-out-of-Atom-id rule as providers/eccc.py::_ATOM_ID_OID_RE; the OID
# is the cross-entry identity, and the feed emits one entry per
# (language × area group) all pointing at the same CAP body.
_ATOM_ID_OID_RE = re.compile(r"urn:oid:[\w.]+")

# CAP <parameter> keys whose values are closed lifecycle/marker sets worth
# tracking as values, keyed by the vocabulary bucket they land in. Matched on
# the unversioned tail so a layer version bump doesn't fork the bucket (the
# bump still shows up as a new parameter *key*).
_ECCC_TRACKED_PARAM_TAILS = {
    ":Alert_Location_Status": "values.Alert_Location_Status",
    ":Alert_Type": "values.Alert_Type",
    ":Colour": "values.Colour",
}

# The CAM threat-area marker (issue #172): geocode valueNames under this
# prefix carry lifecycle-like values (observed: "issued") whose set is exactly
# what we want to watch — an "ended" variant would change how matching should
# treat the area.
_ECCC_DLC_SCHEME_PREFIX = "layer:EC-MSC-SMC:DLC:"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch(url: str, *, timeout: float, retries: int = 3) -> str:
    """GET a URL, returning the body text; raises on persistent failure.

    Bodies carrying a DOCTYPE are refused before any XML parse: stdlib
    ElementTree has no entity-expansion guard (the integration proper uses
    defusedxml), and none of these feeds legitimately declares one.
    """
    last_err: Exception | None = None
    for _ in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise  # an answer, not a transient failure; callers may expect it
            last_err = err
            continue
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            last_err = err
            continue
        if "<!DOCTYPE" in body[:1024]:
            raise ValueError(f"refusing DOCTYPE-bearing body from {url}")
        return body
    raise RuntimeError(f"fetch failed for {url}: {last_err}")


def fetch_feed_complete(url: str, *, timeout: float, retries: int = 3) -> str:
    """Fetch an Atom feed, retrying truncated bodies.

    The alertready.ca feed is chunked with no Content-Length and can arrive
    partial without a transport error (see providers/eccc.py); an incomplete
    document is retried rather than parsed at a random offset.
    """
    last_len = -1
    for _ in range(retries):
        body = fetch(url, timeout=timeout, retries=1)
        if body.rstrip().endswith("</feed>"):
            return body
        last_len = len(body)
    raise RuntimeError(f"truncated feed from {url} ({last_len} bytes)")


# ---------------------------------------------------------------------------
# Vocabulary extraction
# ---------------------------------------------------------------------------

Vocab = dict[str, set[str]]
# (bucket, token) -> distinct alert ids seen with it, first-seen order (a dict
# used as an ordered set).
Witnesses = dict[tuple[str, str], dict[str, None]]


class Sample:
    """One provider's observed vocabulary and the alerts that carried it.

    Envelope-level vocabulary (feed metadata, catalog entries) is added with
    no alert id and reports bare; everything read out of an alert document is
    attributed to that alert's id so the drift report can name it (#184).
    """

    def __init__(self) -> None:
        self.vocab: Vocab = {}
        self.witnesses: Witnesses = {}

    def add(self, bucket: str, value: str | None, alert_id: str | None = None) -> None:
        token = (value or "").strip()
        if not token:
            return
        self.vocab.setdefault(bucket, set()).add(token)
        if alert_id:
            self.witnesses.setdefault((bucket, token), {})[alert_id] = None

    def add_all(
        self, bucket: str, values: Iterable[str], alert_id: str | None = None
    ) -> None:
        for value in values:
            self.add(bucket, value, alert_id)


def _render_tag(tag: str) -> str:
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        prefix = XML_PREFIXES.get(ns)
        return f"{prefix}{local}" if prefix is not None else f"{{{ns}}}{local}"
    return tag


def xml_paths(root: ET.Element, prefix: str = "") -> set[str]:
    """Every element path under ``root``, namespaces rendered as prefixes.

    ``prefix`` is the path of ``root``'s parent, for walking one item of a
    larger document and attributing its paths to that item.
    """
    paths: set[str] = set()

    def walk(el: ET.Element, prefix: str) -> None:
        path = f"{prefix}/{_render_tag(el.tag)}" if prefix else _render_tag(el.tag)
        paths.add(path)
        for child in el:
            walk(child, path)

    walk(root, prefix)
    return paths


def json_paths(
    obj: object, prefix: str = "", *, opaque: frozenset[str] = frozenset()
) -> set[str]:
    """Every key path in a JSON document, arrays collapsed to ``[]``.

    Keys in ``opaque`` are recorded but not descended: NWS ``parameters`` is
    a per-product map whose keys are tracked as ``parameter_keys`` already,
    and walking it would report every one of them a second time as a path.
    """
    paths: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            if str(key) in opaque:
                continue
            paths |= json_paths(value, path, opaque=opaque)
    elif isinstance(obj, list):
        for item in obj:
            paths |= json_paths(item, f"{prefix}[]", opaque=opaque)
    return paths


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def probe_eccc(timeout: float, max_bodies: int, workers: int) -> Sample:
    """NAAD GeoRSS index → sampled CAP bodies → CAP vocabulary.

    alertready.ca only: it is the sanctioned host, retains ~48 h (the deepest
    sample available), and the pelmorex host adds coverage, not vocabulary.
    """
    feed = ET.fromstring(fetch_feed_complete(NAAD_FEED_ALERTREADY, timeout=timeout))

    hrefs: list[str] = []
    seen_oids: set[str] = set()
    for entry in feed.findall(f"{{{NS_ATOM}}}entry"):
        terms = {
            (cat.get("term", "").partition("=")[0].strip()): (
                cat.get("term", "").partition("=")[2].strip()
            )
            for cat in entry.findall(f"{{{NS_ATOM}}}category")
        }
        if terms.get("status") != "Actual":
            continue
        atom_id = entry.findtext(f"{{{NS_ATOM}}}id", "") or ""
        match = _ATOM_ID_OID_RE.search(atom_id)
        oid = match.group(0) if match else atom_id
        if oid in seen_oids:
            continue
        seen_oids.add(oid)
        for link in entry.findall(f"{{{NS_ATOM}}}link"):
            href = link.get("href", "")
            if link.get("type") == "application/cap+xml" or href.lower().endswith(
                (".xml", ".cap")
            ):
                hrefs.append(href)
                break

    hrefs = hrefs[:max_bodies]
    sample = Sample()
    fetched = 0
    skipped_senders = 0

    def fetch_body(href: str) -> str | None:
        try:
            return fetch(href, timeout=timeout, retries=2)
        except Exception:  # noqa: BLE001 — a probe reports None on any failure
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        bodies = list(pool.map(fetch_body, hrefs))

    for body in bodies:
        if body is None:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        fetched += 1
        if (root.findtext(f"{{{NS_CAP}}}sender", "") or "").strip() != ECCC_SENDER:
            skipped_senders += 1
            continue
        alert_id = (root.findtext(f"{{{NS_CAP}}}identifier", "") or "").strip()
        sample.add_all("xml_paths", xml_paths(root), alert_id)
        for tag in ("status", "msgType", "scope"):
            sample.add(f"values.{tag}", root.findtext(f"{{{NS_CAP}}}{tag}"), alert_id)
        for info in root.findall(f"{{{NS_CAP}}}info"):
            # No ``event``/``eventCode`` values: ECCC publishes no catalog to
            # seed them from, so they would trickle in season by season.
            for tag in (
                "language",
                "category",
                "responseType",
                "urgency",
                "severity",
                "certainty",
            ):
                for el in info.findall(f"{{{NS_CAP}}}{tag}"):
                    sample.add(f"values.{tag}", el.text, alert_id)
            for ec in info.findall(f"{{{NS_CAP}}}eventCode"):
                sample.add(
                    "event_code_schemes",
                    ec.findtext(f"{{{NS_CAP}}}valueName"),
                    alert_id,
                )
            for param in info.findall(f"{{{NS_CAP}}}parameter"):
                name = (param.findtext(f"{{{NS_CAP}}}valueName", "") or "").strip()
                sample.add("parameter_keys", name, alert_id)
                for tail, bucket in _ECCC_TRACKED_PARAM_TAILS.items():
                    if name.endswith(tail):
                        sample.add(
                            bucket, param.findtext(f"{{{NS_CAP}}}value"), alert_id
                        )
            for area in info.findall(f"{{{NS_CAP}}}area"):
                for gc in area.findall(f"{{{NS_CAP}}}geocode"):
                    name = (gc.findtext(f"{{{NS_CAP}}}valueName", "") or "").strip()
                    sample.add("geocode_schemes", name, alert_id)
                    if name.startswith(_ECCC_DLC_SCHEME_PREFIX):
                        sample.add(
                            "values.DLC", gc.findtext(f"{{{NS_CAP}}}value"), alert_id
                        )

    if not fetched:
        raise RuntimeError(
            f"no CAP body of {len(hrefs)} sampled could be fetched and parsed"
        )
    print(
        f"  eccc: {len(seen_oids)} Actual OIDs indexed, {fetched} bodies parsed, "
        f"{skipped_senders} non-{ECCC_SENDER} skipped"
    )
    return sample


def _nws_alert_id(feature: dict) -> str:
    return str(feature.get("id") or (feature.get("properties") or {}).get("id") or "")


def _nws_feature(sample: Sample, feature: dict) -> bool:
    """Read one GeoJSON feature into ``sample``; True if its sender is not NWS."""
    props = feature.get("properties", {})
    alert_id = _nws_alert_id(feature)
    sample.add_all(
        "json_paths",
        json_paths(feature, "features[]", opaque=frozenset({"parameters"})),
        alert_id,
    )
    for key in (
        "status",
        "messageType",
        "category",
        "severity",
        "certainty",
        "urgency",
        "response",
    ):
        value = props.get(key)
        if isinstance(value, str):
            sample.add(f"values.{key}", value, alert_id)
    for key in props.get("geocode", {}) or {}:
        sample.add("geocode_keys", str(key), alert_id)
    for key in props.get("eventCode", {}) or {}:
        sample.add("event_code_schemes", str(key), alert_id)
    # Parameter keys from NWS-originated alerts only: an IPAWS relay's
    # parameters are whatever its originator typed (#184).
    if props.get("sender") != NWS_SENDER:
        return True
    for key in props.get("parameters", {}) or {}:
        sample.add("parameter_keys", str(key), alert_id)
    return False


def probe_nws(timeout: float) -> Sample:
    """The active-alerts GeoJSON, the last 48 h of alerts, and the catalog.

    The active endpoint is the one the integration polls, so its envelope is
    the one watched; the history pages contribute alerts only.
    """
    doc = json.loads(fetch(NWS_ACTIVE_URL, timeout=timeout))
    features = doc.get("features", [])
    if not features:
        # A quiet moment nationally is conceivable but has never been observed;
        # far more likely the response shape changed, which IS the finding.
        raise RuntimeError("NWS active feed returned no features")
    sample = Sample()
    # Envelope paths belong to no alert; each feature's paths are attributed
    # to it below.
    sample.add_all("json_paths", json_paths({**doc, "features": []}))
    skipped_senders = 0
    seen: set[str] = set()
    for feature in features:
        seen.add(_nws_alert_id(feature))
        skipped_senders += _nws_feature(sample, feature)

    now = datetime.now(timezone.utc)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    url: str | None = NWS_HISTORY_URL.format(
        start=(now - NWS_LOOKBACK).strftime(stamp), end=now.strftime(stamp)
    )
    history = 0
    for _ in range(NWS_MAX_PAGES):
        if not url:
            break
        page = json.loads(fetch(url, timeout=timeout))
        page_features = page.get("features", []) or []
        if not page_features:
            break
        for feature in page_features:
            alert_id = _nws_alert_id(feature)
            if alert_id in seen:
                continue
            seen.add(alert_id)
            history += 1
            skipped_senders += _nws_feature(sample, feature)
        url = (page.get("pagination") or {}).get("next")

    catalog = json.loads(fetch(NWS_TYPES_URL, timeout=timeout))
    event_types = catalog.get("eventTypes") or []
    if not event_types:
        raise RuntimeError("NWS /alerts/types returned no eventTypes")
    for name in event_types:
        sample.add("values.event", str(name))
    print(
        f"  nws: {len(features)} active alerts + {history} more sent in the last "
        f"{NWS_LOOKBACK.total_seconds() / 3600:.0f} h, {skipped_senders} "
        f"non-{NWS_SENDER} (parameters skipped), {len(event_types)} catalog products"
    )
    return sample


def probe_meteoalarm(timeout: float, workers: int) -> Sample:
    """Every country feed the integration offers; key paths + CAP enums."""
    sample = Sample()
    failures: list[str] = []
    warning_count = 0

    def fetch_country(slug: str) -> tuple[str, dict | None]:
        try:
            return slug, json.loads(
                fetch(METEOALARM_FEED_URL.format(country=slug), timeout=timeout)
            )
        except Exception:  # noqa: BLE001 — a probe reports None on any failure
            return slug, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(fetch_country, sorted(METEOALARM_COUNTRY_SLUGS.values()))
        )

    for slug, doc in results:
        if doc is None:
            failures.append(slug)
            continue
        sample.add_all("json_paths", json_paths({**doc, "warnings": []}))
        for warning in doc.get("warnings", []) or []:
            warning_count += 1
            alert = warning.get("alert", {}) or {}
            alert_id = str(alert.get("identifier") or "")
            sample.add_all("json_paths", json_paths(warning, "warnings[]"), alert_id)
            for tag in ("status", "msgType", "scope"):
                value = alert.get(tag)
                if isinstance(value, str):
                    sample.add(f"values.{tag}", value, alert_id)
            for info in alert.get("info", []) or []:
                # No ``language``: 43 tags on the live hub and a member adding
                # one is not something the integration has to react to.
                for tag in ("category", "severity", "certainty", "urgency"):
                    value = info.get(tag)
                    values = value if isinstance(value, list) else [value]
                    for item in values:
                        if isinstance(item, str):
                            sample.add(f"values.{tag}", item, alert_id)
                # No ``parameter_keys``: beyond the profile's awareness_* pair,
                # parameter names are per member service ("exposed gusts",
                # "direction of approach") — an open set.
                for param in info.get("parameter", []) or []:
                    name = str(param.get("valueName", "")).strip()
                    if name in ("awareness_type", "awareness_level"):
                        code = str(param.get("value", "")).split(";", 1)[0].strip()
                        sample.add(f"values.{name}", code, alert_id)
                for area in info.get("area", []) or []:
                    for gc in area.get("geocode", []) or []:
                        sample.add(
                            "geocode_schemes", str(gc.get("valueName", "")), alert_id
                        )

    if len(failures) == len(results):
        raise RuntimeError("every MeteoAlarm country feed failed")
    note = (
        f", {len(failures)} countries failed ({', '.join(failures)})"
        if failures
        else ""
    )
    print(
        f"  meteoalarm: {len(results) - len(failures)} countries, {warning_count} warnings{note}"
    )
    return sample


def probe_gdacs(timeout: float) -> Sample:
    """Both RSS indexes; item structure plus the closed type/level sets."""
    sample = Sample()
    items = 0
    for url in (GDACS_RSS_CURRENT_URL, GDACS_RSS_24H_URL):
        root = ET.fromstring(fetch(url, timeout=timeout))
        sample.add_all("xml_paths", xml_paths(root))
        for item in root.iter("item"):
            items += 1
            alert_id = (item.findtext("guid", "") or "").strip()
            sample.add_all("xml_paths", xml_paths(item, "rss/channel"), alert_id)
            for tag in ("eventtype", "alertlevel", "episodealertlevel", "iscurrent"):
                sample.add(
                    f"values.{tag}",
                    item.findtext(f"{{http://www.gdacs.org}}{tag}"),
                    alert_id,
                )
    if not items:
        raise RuntimeError("GDACS indexes contained no items")
    print(f"  gdacs: {items} index items across both feeds")
    return sample


def _bbk_row_expired(expires: object, now: datetime) -> bool:
    """True when a mapData row's ``expiresDate`` parses and is in the past."""
    if not isinstance(expires, str) or not expires.strip():
        return False
    try:
        when = datetime.fromisoformat(expires.strip())
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= now


def probe_bbk(timeout: float, max_bodies: int, workers: int) -> Sample:
    """Five channel indexes, then every listed warning document (issue #66).

    The document is CAP-over-JSON, so the vocabulary that matters is the CAP
    enum set per ``info[]`` block plus the one code scheme the integration
    classifies on, DWD's ``GROUP`` eventCode. The scheme names are tracked
    but not the ``profile:DE-BBK-EVENTCODE`` values: the integration passes
    those through as a parameter, and the catalogue runs to some hundred
    hazard codes that would page in one at a time (#217). Id prefixes are
    tracked because a new one is a new channel the provider does not name.
    ``language`` is tracked because the options form offers a closed list of
    them.
    """
    sample = Sample()
    ids: dict[str, str] = {}
    failures: list[str] = []
    now = datetime.now(timezone.utc)
    for channel in BBK_CHANNELS:
        url = BBK_MAPDATA_URL.format(channel=channel)
        try:
            rows = json.loads(fetch(url, timeout=timeout))
        except Exception as err:  # noqa: BLE001 — one channel down is reported, not fatal
            failures.append(f"{channel}: {err}")
            continue
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            warning_id = str(row.get("id") or "")
            sample.add_all("index_paths", json_paths(row, "mapData[]"), warning_id)
            for tag in ("severity", "urgency", "type"):
                value = row.get(tag)
                if isinstance(value, str):
                    sample.add(f"index.{tag}", value, warning_id)
            if warning_id:
                sample.add("values.id_prefix", warning_id.partition(".")[0], warning_id)
                # The index lists a row past its ``expiresDate`` for a while,
                # and the document URL of an expired warning 302s to an
                # archive copy of a different shape (empty ``polygon`` /
                # ``circle`` / ``geocode`` / ``resource`` keys, a relabelled
                # headline). The provider drops such rows before fetching
                # (``bbk._live_entries``), so the probe does too (#217).
                if not _bbk_row_expired(row.get("expiresDate"), now):
                    ids.setdefault(warning_id, channel)
    if len(failures) == len(BBK_CHANNELS):
        raise RuntimeError(f"every BBK channel index failed: {'; '.join(failures)}")

    def fetch_doc(warning_id: str) -> tuple[str, dict | None]:
        try:
            return warning_id, json.loads(
                fetch(BBK_WARNING_URL.format(warning_id=warning_id), timeout=timeout)
            )
        except Exception:  # noqa: BLE001 — a probe reports None on any failure
            return warning_id, None

    sampled = sorted(ids)[:max_bodies]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        docs = list(pool.map(fetch_doc, sampled))

    fetched = 0
    for warning_id, doc in docs:
        if not isinstance(doc, dict):
            continue
        fetched += 1
        sample.add_all("json_paths", json_paths(doc), warning_id)
        for tag in ("status", "msgType", "scope"):
            value = doc.get(tag)
            if isinstance(value, str):
                sample.add(f"values.{tag}", value, warning_id)
        for info in doc.get("info", []) or []:
            if not isinstance(info, dict):
                continue
            for tag in (
                "language",
                "category",
                "severity",
                "certainty",
                "urgency",
                "responseType",
            ):
                value = info.get(tag)
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if isinstance(item, str):
                        sample.add(f"values.{tag}", item, warning_id)
            for code in info.get("eventCode", []) or []:
                name = str(code.get("valueName", "")).strip()
                sample.add("eventcode_names", name, warning_id)
                if name == "GROUP":
                    sample.add(f"values.{name}", str(code.get("value", "")), warning_id)
            for area in info.get("area", []) or []:
                for gc in area.get("geocode", []) or []:
                    sample.add(
                        "geocode_schemes", str(gc.get("valueName", "")), warning_id
                    )

    note = (
        f", {len(failures)} channels failed ({'; '.join(failures)})" if failures else ""
    )
    print(f"  bbk: {len(ids)} index entries, {fetched} documents fetched{note}")
    return sample


# ---------------------------------------------------------------------------
# WMO: mirror lag (issue #210)
# ---------------------------------------------------------------------------

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _fetch_optional(url: str, *, timeout: float) -> str | None:
    """``fetch``, except a 404 returns ``None`` instead of raising."""
    try:
        return fetch(url, timeout=timeout)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def _s3_prefixes(registry: object) -> list[str]:
    """The registry's language feeds hosted on the cap-sources bucket.

    A registry record lists one ``capAlertFeed`` URL per language, comma
    separated. Only bucket-hosted feeds are listable; the ~70 on national
    hosts are of no common shape and are left to the human half.
    """
    sources = registry.get("sources") if isinstance(registry, dict) else None
    prefixes: set[str] = set()
    for entry in sources or []:
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, dict):
            continue
        for url in str(source.get("capAlertFeed") or "").split(","):
            match = re.match(rf"{re.escape(CAP_SOURCES_BUCKET)}([^/]+)/", url.strip())
            if match:
                prefixes.add(match.group(1))
    return sorted(prefixes)


def _mirror_newest(prefix: str, timeout: float) -> tuple[int, datetime | None] | None:
    """Item count and newest ``pubDate`` on the mirror; ``None`` if it 404s."""
    body = _fetch_optional(WMO_MIRROR_RSS_URL.format(source_id=prefix), timeout=timeout)
    if body is None:
        return None
    items = list(ET.fromstring(body).iter("item"))
    newest: datetime | None = None
    for item in items:
        text = (item.findtext("pubDate") or "").strip()
        try:
            published = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if newest is None or published > newest:
            newest = published
    return len(items), newest


def _s3_alert_keys(prefix: str, timeout: float) -> list[tuple[datetime, str]]:
    """Every alert file under the prefix as (last modified, key), oldest first.

    The folder also holds the feed's own ``rss.xml`` and two stylesheets;
    everything else is one CAP document per alert.
    """
    keys: list[tuple[datetime, str]] = []
    token: str | None = None
    while True:
        url = f"{CAP_SOURCES_BUCKET}?list-type=2&prefix={prefix}/"
        if token:
            url += f"&continuation-token={urllib.parse.quote(token)}"
        root = ET.fromstring(fetch(url, timeout=timeout))
        for entry in root.iter(f"{{{S3_NS}}}Contents"):
            key = entry.findtext(f"{{{S3_NS}}}Key") or ""
            modified = entry.findtext(f"{{{S3_NS}}}LastModified") or ""
            if not key.endswith(".xml") or key.endswith("/rss.xml"):
                continue
            keys.append((datetime.fromisoformat(modified.replace("Z", "+00:00")), key))
        if root.findtext(f"{{{S3_NS}}}IsTruncated") != "true":
            break
        token = root.findtext(f"{{{S3_NS}}}NextContinuationToken")
    keys.sort()
    return keys


def _newest_actual(
    keys: list[tuple[datetime, str]], timeout: float
) -> tuple[datetime, str] | None:
    """The newest ``status=Actual`` document among the last few uploaded."""
    for modified, key in reversed(keys[-WMO_LAG_CONFIRM_BODIES:]):
        try:
            root = ET.fromstring(fetch(CAP_SOURCES_BUCKET + key, timeout=timeout))
        except ET.ParseError:
            continue
        if (root.findtext("{*}status") or "").strip() == "Actual":
            return modified, key
    return None


def _check_mirror(
    prefix: str, timeout: float
) -> tuple[str, str, str | None, str | None]:
    """Classify one feed: (prefix, verdict, token, witness URL).

    Verdicts: ``unmirrored`` (the mirror 404s), ``quiet`` (no Actual alert
    among the bucket's settled files), ``settling`` (every file is younger
    than ``WMO_MIRROR_LAG``, so the mirror has not had its grace yet),
    ``fresh``, ``dormant`` (behind, but the authority has not published
    within ``WMO_SOURCE_ACTIVE``), or ``lag``.

    Only files the mirror has had ``WMO_MIRROR_LAG`` to pick up are judged,
    and the mirror is fresh when its newest item is no older than the newest
    of them: the mirror stamps an item with its ingestion time, minutes
    after the upload. The bucket's upload times gate the comparison so
    bodies are only read back for a feed that already looks behind and
    whose source is awake.
    """
    mirror = _mirror_newest(prefix, timeout)
    if mirror is None:
        return prefix, "unmirrored", None, None
    _, mirror_newest = mirror
    keys = _s3_alert_keys(prefix, timeout)
    if not keys:
        return prefix, "quiet", None, None
    now = datetime.now(timezone.utc)
    settled = [
        (uploaded, key) for uploaded, key in keys if now - uploaded > WMO_MIRROR_LAG
    ]
    if not settled:
        return prefix, "settling", None, None
    if mirror_newest is not None and mirror_newest >= settled[-1][0]:
        return prefix, "fresh", None, None
    if now - settled[-1][0] > WMO_SOURCE_ACTIVE:
        return prefix, "dormant", None, None
    actual = _newest_actual(settled, timeout)
    if actual is None:
        return prefix, "quiet", None, None
    uploaded, key = actual
    if mirror_newest is not None and mirror_newest >= uploaded:
        return prefix, "fresh", None, None
    if now - uploaded > WMO_SOURCE_ACTIVE:
        return prefix, "dormant", None, None
    stopped = f"{mirror_newest:%Y-%m-%d}" if mirror_newest else "never"
    return prefix, "lag", f"{prefix}@{stopped}", CAP_SOURCES_BUCKET + key


def probe_wmo(timeout: float, workers: int) -> Sample:
    """SWIC mirror feeds that have stopped following their cap-sources bucket.

    Not vocabulary: a ``mirror_lag`` token names a feed and the date of the
    mirror's newest item, witnessed by the newest Actual alert the source
    has published since. Only feeds whose authority published within
    ``WMO_SOURCE_ACTIVE`` are reported; the rest are tallied as dormant on
    stdout. The baseline holds the acknowledged laggards; a mirror that
    recovers and stalls again carries a new date and is drift.
    """
    registry = json.loads(fetch(WMO_SOURCES_URL, timeout=timeout))
    prefixes = _s3_prefixes(registry)
    if not prefixes:
        raise RuntimeError("SWIC registry listed no cap-sources feeds")

    def check(prefix: str) -> tuple[str, str, str | None, str | None]:
        try:
            return _check_mirror(prefix, timeout)
        except Exception as err:  # noqa: BLE001 — one feed must not end the sweep
            return prefix, "error", None, str(err)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(check, prefixes))

    errors = [
        (prefix, detail) for prefix, verdict, _, detail in results if verdict == "error"
    ]
    if len(errors) > len(prefixes) // 10:
        shown = "; ".join(f"{prefix}: {detail}" for prefix, detail in errors[:3])
        raise RuntimeError(
            f"{len(errors)} of {len(prefixes)} feeds unreachable ({shown})"
        )

    sample = Sample()
    tally: dict[str, int] = {}
    for prefix, verdict, token, witness in results:
        tally[verdict] = tally.get(verdict, 0) + 1
        if verdict == "lag":
            sample.add("mirror_lag", token, witness)
        elif verdict == "error":
            print(f"  wmo: skipped {prefix}: {witness}")
    print(
        f"  wmo: {len(prefixes)} cap-sources feeds: "
        + ", ".join(f"{count} {verdict}" for verdict, count in sorted(tally.items()))
    )
    return sample


# ---------------------------------------------------------------------------
# Baseline diffing
# ---------------------------------------------------------------------------


def load_baseline(path: Path) -> dict[str, dict[str, list[str]]]:
    data = json.loads(path.read_text())
    data.pop("_meta", None)
    return data


def diff_vocab(
    baseline: dict[str, list[str]] | None, observed: Vocab
) -> dict[str, list[str]]:
    """New tokens per bucket. Absence from a run is never drift."""
    known = {bucket: set(tokens) for bucket, tokens in (baseline or {}).items()}
    drift = {
        bucket: sorted(tokens - known.get(bucket, set()))
        for bucket, tokens in observed.items()
    }
    return {bucket: new for bucket, new in drift.items() if new}


def merge_vocab(
    baseline: dict[str, list[str]] | None, observed: Vocab
) -> dict[str, list[str]]:
    merged = {bucket: set(tokens) for bucket, tokens in (baseline or {}).items()}
    for bucket, tokens in observed.items():
        merged.setdefault(bucket, set()).update(tokens)
    return {bucket: sorted(tokens) for bucket, tokens in sorted(merged.items())}


def _render_token(token: str, alert_ids: dict[str, None] | None) -> str:
    if not alert_ids:
        return f"`{token}`"
    shown = ", ".join(f"`{alert_id}`" for alert_id in list(alert_ids)[:MAX_WITNESS_IDS])
    if len(alert_ids) > MAX_WITNESS_IDS:
        shown += ", …"
    noun = "alert" if len(alert_ids) == 1 else "alerts"
    return f"`{token}` in {len(alert_ids)} {noun}: {shown}"


def build_report(
    drift: dict[str, dict[str, list[str]]],
    failures: dict[str, str],
    witnesses: dict[str, Witnesses] | None = None,
) -> str:
    """GitHub-issue-ready markdown for whatever the run found.

    Each new token lists the first few alert ids that carried it, so the
    reader can open the documents while the feed still has them.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"Provider feed probe, {today}."]
    for provider, buckets in sorted(drift.items()):
        seen = (witnesses or {}).get(provider, {})
        lines.append("")
        lines.append(f"### {provider}")
        for bucket, tokens in sorted(buckets.items()):
            lines.append(f"- **{bucket}**")
            for token in tokens:
                lines.append(f"  - {_render_token(token, seen.get((bucket, token)))}")
    if failures:
        lines.append("")
        lines.append("### Probe failures")
        for provider, error in sorted(failures.items()):
            lines.append(f"- **{provider}**: {error}")
    lines.append("")
    lines.append(
        "New tokens mean the provider changed something. Check the provider's "
        "announcement channel (docs/provider-watch.md), decide whether the "
        "integration needs to react, then accept the vocabulary with "
        "`scripts/feed_vocab_probe.py --update` and PR the baseline change. "
        "A token that has aged out of the feed by then goes into the baseline "
        "by hand."
    )
    if "wmo" in drift:
        lines.append("")
        lines.append(
            "A `wmo` mirror_lag token is a SWIC mirror feed that has stopped "
            "following its cap-sources bucket (#210): the date is the mirror's "
            "newest item, the alert is the newest Actual one the source has "
            "published since. Users of that source see an empty feed, not an "
            "error. Report it to SWIC, note it under the WMO section of "
            "docs/architecture.md, and accept the token the same way; a mirror "
            "that recovers and stalls again gets a new date and a fresh token."
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def probe_au(timeout: float) -> Sample:
    """The four state feeds, every alert in each (issue #127).

    Each feed is one EDXL-DE document, so there is nothing to sample: every
    ``<alert>`` is read. Tracked beyond the CAP enums are the parameter keys
    (three feeds carry the tier in ``AlertLevel``; a fourth starting to would
    change how WA is read), the tier values themselves, the govshare event
    codes, geocode schemes and the marker-circle radii — the provider reads
    circles up to 0.5 km as points, so a feed moving its marker radius shows
    up here. ``IncidentType`` is a parameter key only: its values are each
    agency's dispatch dictionary, which filled in one rare member at a time
    over four rounds in five days (#217, #235, #238), and the one reader, the
    icon classifier, degrades to the fallback icon on a member it has no
    needle for.
    """
    sample = Sample()
    failures: list[str] = []
    alerts = 0
    for state, url in AU_FEEDS.items():
        try:
            root = ET.fromstring(fetch(url, timeout=timeout))
        except Exception as err:  # noqa: BLE001 — one state down is reported, not fatal
            failures.append(f"{state}: {err}")
            continue
        sample.add_all("envelope_paths", xml_paths(root))
        for alert in root.iter(f"{{{NS_CAP}}}alert"):
            alerts += 1
            alert_id = (
                f"{state}:{(alert.findtext(f'{{{NS_CAP}}}identifier') or '').strip()}"
            )
            sample.add_all("xml_paths", xml_paths(alert), alert_id)
            for tag in ("status", "msgType", "scope"):
                sample.add(
                    f"values.{tag}", alert.findtext(f"{{{NS_CAP}}}{tag}"), alert_id
                )
            for info in alert.findall(f"{{{NS_CAP}}}info"):
                for tag in (
                    "language",
                    "category",
                    "responseType",
                    "urgency",
                    "severity",
                    "certainty",
                ):
                    for el in info.findall(f"{{{NS_CAP}}}{tag}"):
                        sample.add(f"values.{tag}", el.text, alert_id)
                for ec in info.findall(f"{{{NS_CAP}}}eventCode"):
                    sample.add(
                        "event_code_schemes",
                        ec.findtext(f"{{{NS_CAP}}}valueName"),
                        alert_id,
                    )
                    sample.add(
                        "values.eventCode", ec.findtext(f"{{{NS_CAP}}}value"), alert_id
                    )
                for param in info.findall(f"{{{NS_CAP}}}parameter"):
                    name = (param.findtext(f"{{{NS_CAP}}}valueName", "") or "").strip()
                    sample.add("parameter_keys", name, alert_id)
                    if name == "AlertLevel":
                        sample.add(
                            f"values.{name}",
                            param.findtext(f"{{{NS_CAP}}}value"),
                            alert_id,
                        )
                for area in info.findall(f"{{{NS_CAP}}}area"):
                    for gc in area.findall(f"{{{NS_CAP}}}geocode"):
                        sample.add(
                            "geocode_schemes",
                            gc.findtext(f"{{{NS_CAP}}}valueName"),
                            alert_id,
                        )
                    for circle in area.findall(f"{{{NS_CAP}}}circle"):
                        parts = (circle.text or "").split()
                        if len(parts) == 2:
                            sample.add("values.circle_radius", parts[1], alert_id)
    if len(failures) == len(AU_FEEDS):
        raise RuntimeError(f"every AU state feed failed: {'; '.join(failures)}")
    note = f", {len(failures)} feeds failed ({'; '.join(failures)})" if failures else ""
    print(f"  au: {alerts} alerts across {len(AU_FEEDS) - len(failures)} feeds{note}")
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe the live provider feeds: diff their vocabulary against the "
            "baseline and check each WMO mirror against its source."
        )
    )
    parser.add_argument(
        "--providers",
        default=",".join(PROVIDERS),
        help=f"comma-separated subset of: {', '.join(PROVIDERS)}",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="fold observed vocabulary into the baseline instead of failing on it",
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="write a markdown report here when there is drift or a failure",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--max-bodies",
        type=int,
        default=400,
        help="cap on ECCC CAP bodies and BBK documents sampled per run",
    )
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    requested = [p.strip() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in requested if p not in PROVIDERS]
    if unknown:
        parser.error(f"unknown providers: {', '.join(unknown)}")

    if args.baseline.exists():
        baseline = load_baseline(args.baseline)
    elif args.update:
        baseline = {}
    else:
        print(f"error: no baseline at {args.baseline}; seed one with --update")
        return 2

    observed: dict[str, Sample] = {}
    failures: dict[str, str] = {}
    for provider in requested:
        print(f"probing {provider}…")
        try:
            if provider == "eccc":
                observed[provider] = probe_eccc(
                    args.timeout, args.max_bodies, args.workers
                )
            elif provider == "nws":
                observed[provider] = probe_nws(args.timeout)
            elif provider == "meteoalarm":
                observed[provider] = probe_meteoalarm(args.timeout, args.workers)
            elif provider == "gdacs":
                observed[provider] = probe_gdacs(args.timeout)
            elif provider == "wmo":
                observed[provider] = probe_wmo(args.timeout, args.workers)
            elif provider == "bbk":
                observed[provider] = probe_bbk(
                    args.timeout, args.max_bodies, args.workers
                )
            elif provider == "au":
                observed[provider] = probe_au(args.timeout)
        except Exception as err:  # noqa: BLE001 — one provider down must not end the run
            failures[provider] = str(err)
            print(f"  FAILED: {err}")

    drift = {
        provider: found
        for provider, sample in observed.items()
        if (found := diff_vocab(baseline.get(provider), sample.vocab))
    }

    if args.update:
        merged = {
            provider: merge_vocab(
                merge_vocab(baseline.get(provider), sample.vocab),
                {
                    bucket: set(tokens)
                    for bucket, tokens in SPEC_VOCAB.get(provider, {}).items()
                },
            )
            for provider, sample in observed.items()
        }
        # Providers not probed this run keep their existing sections.
        for provider, buckets in baseline.items():
            merged.setdefault(provider, buckets)
        payload: dict[str, object] = {
            "_meta": {
                "comment": (
                    "Known provider feed vocabulary; grown by "
                    "scripts/feed_vocab_probe.py --update, never pruned."
                ),
            }
        }
        payload.update(dict(sorted(merged.items())))
        args.baseline.write_text(json.dumps(payload, indent=1) + "\n")
        print(f"baseline updated: {args.baseline}")

    if drift or failures:
        report = build_report(
            drift, failures, {p: s.witnesses for p, s in observed.items()}
        )
        print()
        print(report, end="")
        if args.report_file:
            args.report_file.write_text(report)

    if failures:
        return 2
    if drift and not args.update:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
