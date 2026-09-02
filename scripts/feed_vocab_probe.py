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

WMO is not probed: the integration polls per-configured-source RSS and no
bounded national endpoint exists, so a probe would either sample arbitrary
sources or walk all ~140. Announcement channels cover it (see
docs/provider-watch.md).

Usage:
    scripts/feed_vocab_probe.py                    # probe all, diff vs baseline
    scripts/feed_vocab_probe.py --update           # fold new vocabulary in
    scripts/feed_vocab_probe.py --providers eccc,nws
    scripts/feed_vocab_probe.py --report-file drift.md   # issue-ready markdown

Exit status:
    0  no drift
    1  drift found (cron-friendly: non-zero means "look at the report")
    2  a provider could not be probed at all (drift, if any, still reported)

Run daily by .github/workflows/feed-vocab.yml, which opens/updates a
``provider-drift`` issue from the report (skipping a comment that would only
repeat tokens already on the open issue). To accept drift, review it, then
run ``--update`` and PR the baseline change.

Stdlib only — run with system python3, no venv needed. (It loads
``custom_components/cap_alerts/const.py`` by path; that file is pure Python.)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# const.py is pure Python but sits behind a package __init__ that imports
# homeassistant, so it is loaded by file path under a private name. That
# pattern is banned in tests/ (import hygiene, #137) because a second copy
# shadows the real module for the rest of a pytest session; this is a
# standalone process that never sees the real one.
import importlib.util  # noqa: E402

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

BASELINE_PATH = Path(__file__).resolve().parent / "feed_vocab_baseline.json"

USER_AGENT = "cap-alerts-feed-vocab-probe/1.0 (+https://github.com/seevee/cap_alerts)"

PROVIDERS = ("eccc", "nws", "meteoalarm", "gdacs")

# ECCC vocabulary is read from this sender only; see module docstring.
ECCC_SENDER = "cap-pac@canada.ca"
# NWS parameter keys likewise; the active feed also carries IPAWS relays.
NWS_SENDER = "w-nws.webmaster@noaa.gov"

# Alert ids printed per new token in the drift report.
MAX_WITNESS_IDS = 3

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
        except Exception:
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
        except Exception:
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
    lines = [f"Feed vocabulary probe, {today}."]
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
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff live provider feed vocabulary against the baseline."
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
        help="cap on ECCC CAP bodies sampled per run",
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
