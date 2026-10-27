"""WMO Severe Weather Information Centre (SWIC) provider.

Two-step fetch: pull a per-source feed of links, extract the per-item CAP XML
URLs, fetch those via the shared ``CAPContentCache``, and parse standard CAP
1.2 XML into ``CAPAlert`` objects.

Two feeds carry a source (issue #210). The registry record names the feed
the authority itself publishes (``capAlertFeed``): a folder on WMO's
cap-sources bucket for ~120 language feeds, a national host for the rest. The
SWIC mirror served ``…/cap-alerts/{source_id}/rss.xml`` beside it until SWIC
retired those feeds on 2026-10-08 (issue #333); only ``cn-cma-xx`` and
``au-bom-en`` still answer. ``feed_source`` ``auto`` reads the authority feed
first and unions the mirror in by CAP identifier, backing a mirror that keeps
answering 404 off to a slow probe; ``mirror`` / ``authority`` pin one. Both
feed shapes are an index of links, RSS 2.0 (``<item><link>`` text) or Atom
(``<entry><link href>``), and the CAP body is the record either way.

The CAP body parsing is shared with ECCC via the provider-neutral ``cap``
module: ``parse_cap_alert`` is namespace-agnostic and handles the
``urn:oasis:names:tc:emergency:cap:1.2`` namespace WMO feeds use, and the
``CAPDoc`` / ``CAPInfoDoc`` containers and ``resolve_chain_leaves`` revision
logic are reused verbatim by both this provider and ECCC.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
from defusedxml import ElementTree as ET

from ..const import (
    CONF_FEED_SOURCE,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_SOURCE_ID,
    DEFAULT_WMO_FEED_SOURCE,
    WMO_CAP_SOURCES_BUCKET,
    WMO_HEAVY_INDEX_BYTES,
    WMO_HEAVY_INDEX_INTERVAL,
    WMO_HEAVY_INDEX_STAND_IN_LIMIT,
    WMO_MIRROR_GONE_STREAK,
    WMO_SOURCES_URL,
)
from ..conventions import SourceConventions, parse_instant
from ..model import CAPAlert, geocodes_from
from . import ProviderError
from .cap import (
    CAPDoc,
    CAPInfoDoc,
    parse_cap_alert,
    resolve_chain_leaves,
    select_alt_info,
    select_info,
)
from .cap_content_cache import CAPContentCache
from .geometry import geometry_from_shapes, points_from_circles
from .gps import alert_polygons, parse_gps, point_in_polygon
from .revalidate import FeedStatusError, FeedValidators, fetch_feed_text
from .wmo_conventions import CONVENTIONS as _CONVENTIONS

_LOGGER = logging.getLogger(__name__)

WMO_RSS_URL = "https://severeweather.wmo.int/v2/cap-alerts/{source_id}/rss.xml"

# The feed names used in log lines, ``ProviderError`` text, and the
# per-feed warning streaks; ``feed_source`` pins by the same names.
FEED_MIRROR = "mirror"
FEED_AUTHORITY = "authority"

_ATOM_NS = "http://www.w3.org/2005/Atom"


def _utcnow() -> datetime:
    """The poll clock, a seam so tests can pin a captured feed in time."""
    return datetime.now(timezone.utc)


# Buddhist-Era calendar correction. Thailand's TMD, relayed by SWIC, emits
# Buddhist-Era years — Gregorian + 543 — in CAP dateTime fields, e.g.
# "2568-08-05T22:50:00+07:00". Left as-is, ``_compute_phase`` never expires the
# alert and the card renders "STARTS IN 198034d". A year at or above this
# threshold is unambiguously BE: no Gregorian weather alert is ~375 years out,
# while every BE year is 2543+, so the offset can be subtracted with no risk to
# a valid timestamp. Applied to the CAP-body ISO strings at construction
# (``_gregorian``) and to the RSS envelope's RFC-2822 ``cap:expires``
# (``_gregorian_year``), which the pre-filter reads before any body is built.
MIN_BUDDHIST_ERA_YEAR = 2400
BUDDHIST_ERA_OFFSET = 543

# A CAP dateTime's leading 4-digit year and everything after it.
_ISO_YEAR_RE = re.compile(r"^(\d{4})(\D.*)$")


def _gregorian(value: str) -> str:
    """Rewrite a Buddhist-Era year in a CAP dateTime to Gregorian.

    Returns ``value`` unchanged when it lacks a leading 4-digit year or the
    year is already Gregorian (< 2400). Only the year is touched; month, day,
    time, and UTC offset are preserved verbatim — the Thai solar calendar is
    Gregorian apart from the era number (BE = CE + 543).
    """
    if not value:
        return value
    m = _ISO_YEAR_RE.match(value)
    if m is None:
        return value
    year = int(m.group(1))
    if year < MIN_BUDDHIST_ERA_YEAR:
        return value
    return f"{year - BUDDHIST_ERA_OFFSET:04d}{m.group(2)}"


# ---------------------------------------------------------------------------
# Feed envelope parsing (RSS 2.0 and Atom)
# ---------------------------------------------------------------------------


def _gregorian_year(dt: datetime) -> datetime | None:
    """Correct a Buddhist-Era year on a parsed datetime to Gregorian.

    Thai feeds (TMD) emit BE years (Gregorian + 543) in the RSS envelope's
    RFC-2822 ``cap:expires`` too, not just the CAP body. Without this the
    pre-filter reads every Thai alert as ~543 years in the future and never
    drops the expired ones. Returns the datetime unchanged when its year is
    already Gregorian, or ``None`` if the corrected date is invalid (a
    BE-labelled 29 Feb with no Gregorian counterpart) so the caller fails open.
    """
    if dt.year < MIN_BUDDHIST_ERA_YEAR:
        return dt
    try:
        return dt.replace(year=dt.year - BUDDHIST_ERA_OFFSET)
    except ValueError:
        return None


def _item_expires(item: Any) -> datetime | None:
    """Parse an RSS item's CAP ``expires`` extension (namespace-agnostic).

    The WMO mirror enriches each ``<item>`` with CAP-namespace elements
    (``cap:expires``, ``cap:severity``, …). Returns the expiry as an aware
    ``datetime`` (naive values are assumed UTC, Buddhist-Era years corrected
    to Gregorian), or ``None`` when the element is absent or unparseable.
    """
    for child in item:
        if child.tag.rsplit("}", 1)[-1] != "expires" or not child.text:
            continue
        try:
            dt = parsedate_to_datetime(child.text.strip())
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _gregorian_year(dt)
    return None


# An RSS <guid> whose leading digits are the item's area code followed by a
# per-alert serial, then "_<timestamp>" — e.g. "52272741600000_20260804103516",
# where 522727 is Pingtang County (GB/T 2260) and the CAP body's geocode is
# "522727000000". Observed on every one of cn-cma-xx's 500 items (2026-08-04).
# Used only to skip CAP-body fetches that the geocode filter would discard
# anyway; see _prefilter_by_guid for why this can never drop a wanted alert.
_GUID_AREA_CODE_RE = re.compile(r"^(\d{6})\d*_\d+$")

# Width of the area code embedded in the guid above. Prefixes longer than this
# reach into the serial digits, where guid and geocode diverge, so the
# pre-filter disengages rather than guess.
_GUID_AREA_CODE_WIDTH = 6


def _prefilter_by_guid(items: list[Any], prefixes: Sequence[str]) -> set[int] | None:
    """Indices of items whose guid area code matches a prefix, or ``None``.

    A pure optimization layered under the authoritative post-fetch geocode
    filter: it exists so a narrowed entry does not pay for CAP bodies it is
    about to discard. cn-cma-xx publishes 501 CAP URLs per poll at ~89 KiB
    each, which costs ~32 s and blows the default 30 s timeout; with a
    province prefix this fetches ~30 of them instead.

    Returns ``None`` — meaning "fetch everything, decide after parsing" —
    whenever the guid cannot be trusted to answer the question, so the
    optimization can only ever be lossless:

    * any configured prefix is non-numeric or longer than the embedded area
      code, where guid and geocode provably diverge (a full 12-digit code
      matches the body's ``130709000000`` but never the guid's
      ``13070941600000``);
    * any item's guid does not match the expected shape, i.e. this is not a
      feed whose guids carry area codes;
    * the filter would keep nothing, which is far more likely to mean the guid
      convention changed than that the user has zero alerts.

    Under those guards a kept/dropped decision on ``guid[:6]`` is identical to
    one on ``geocode[:6]``, because both are the same GB/T 2260 code.
    """
    wanted = [p.strip() for p in prefixes if p and p.strip()]
    if not wanted:
        return None
    if any(not p.isdigit() or len(p) > _GUID_AREA_CODE_WIDTH for p in wanted):
        return None

    kept: set[int] = set()
    for index, item in enumerate(items):
        guid = (item.findtext("guid") or "").strip()
        match = _GUID_AREA_CODE_RE.match(guid)
        if match is None:
            return None
        code = match.group(1)
        if any(code.startswith(p) for p in wanted):
            kept.add(index)
    return kept or None


def _atom_entry_href(entry: Any) -> str | None:
    """The CAP document URL of an Atom ``<entry>``.

    A ``<link>`` typed ``application/cap+xml`` wins (DWD carries one beside
    its ``alternate``); otherwise the first ``alternate`` or untyped link.
    Hub and self links on the entry are skipped by their ``rel``.
    """
    alternate: str | None = None
    for link in entry.findall(f"{{{_ATOM_NS}}}link"):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        if "cap+xml" in (link.get("type") or ""):
            return href
        if (link.get("rel") or "alternate") == "alternate" and alternate is None:
            alternate = href
    return alternate


def _parse_feed_links(
    xml_text: str,
    *,
    now: datetime | None = None,
    geocode_prefixes: Sequence[str] | None = None,
) -> list[str]:
    """Extract the per-item CAP XML URLs of a feed, for currently-active alerts.

    Two envelope shapes. RSS 2.0 (the mirror, the cap-sources bucket, and
    about half the national hosts): ``<link>`` is a plain-text element. Atom
    (DWD, Belhydromet, the other half): the URL is a ``<link href>``
    attribute, see ``_atom_entry_href``.

    Items whose CAP ``expires`` extension is already in the past are skipped,
    so high-volume feeds (PAGASA lists ~500 items, nearly all expired) only
    trigger CAP-body fetches for live alerts — without this the cold-start
    cascade exceeds the coordinator poll timeout. Items lacking a parseable
    ``expires`` are kept (fail-open): the bucket and national feeds carry no
    ``cap:`` extensions at all, and the body-level filter in ``async_fetch``
    does the work there. Raises ``ET.ParseError`` on malformed XML — the
    caller converts that to ``ProviderError``. Returns ``[]`` for a feed with
    no live items.
    """
    cutoff = now or _utcnow()
    root = ET.fromstring(xml_text)
    if root.tag == f"{{{_ATOM_NS}}}feed":
        hrefs = (_atom_entry_href(e) for e in root.findall(f"{{{_ATOM_NS}}}entry"))
        return [href for href in hrefs if href]
    items = list(root.iter("item"))
    # Optional area-code gate, applied before any CAP body is fetched. Returns
    # None whenever the guid cannot answer the question, in which case every
    # item is kept and the post-fetch geocode filter decides as before.
    allowed = _prefilter_by_guid(items, geocode_prefixes or ())
    links: list[str] = []
    for index, item in enumerate(items):
        if allowed is not None and index not in allowed:
            continue
        link = item.findtext("link")
        if not (link and link.strip()):
            continue
        expires = _item_expires(item)
        if expires is not None and expires < cutoff:
            continue
        links.append(link.strip())
    return links


# ---------------------------------------------------------------------------
# Source registry: the config-flow dropdown and the authority feed resolver
# ---------------------------------------------------------------------------


async def _registry_payload(
    session: aiohttp.ClientSession, *, user_agent: str | None = None
) -> Any | None:
    """The SWIC registry (``WMO_SOURCES_URL``) as parsed JSON, or ``None``.

    ``None`` means the registry could not be read (HTTP error, network
    error, bad JSON); callers treat that as "unknown for now", never as "no
    such source". The registry is served from CloudFront edge copies and only
    404s when an edge's copy expires during a full origin outage, so a retry
    on the next poll closes that window.
    """
    headers = {"User-Agent": user_agent} if user_agent else None
    try:
        async with session.get(WMO_SOURCES_URL, headers=headers) as resp:
            if resp.status != 200:
                return None
            try:
                return await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return None
    except aiohttp.ClientError:
        return None


def _registry_records(payload: Any) -> list[Mapping[str, Any]]:
    """The ``source`` records of a registry payload that carry a ``sourceId``.

    An unexpected shape yields ``[]`` rather than raising, so a changed
    registry degrades to "no sources" / "no authority feed" everywhere.
    """
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        return []
    records: list[Mapping[str, Any]] = []
    for entry in sources:
        if not isinstance(entry, Mapping):
            continue
        source = entry.get("source")
        if isinstance(source, Mapping) and str(source.get("sourceId") or "").strip():
            records.append(source)
    return records


def _record_feed_urls(record: Mapping[str, Any]) -> list[str]:
    """A record's ``capAlertFeed`` URLs: 51 of 140 list several, comma-joined."""
    raw = str(record.get("capAlertFeed") or "")
    return [url.strip() for url in raw.split(",") if url.strip()]


_BUCKET_FOLDER_RE = re.compile(re.escape(WMO_CAP_SOURCES_BUCKET) + r"([^/]+)/rss\.xml$")


def _bucket_folder(url: str) -> str | None:
    """The cap-sources folder a feed URL names, or ``None`` off the bucket."""
    match = _BUCKET_FOLDER_RE.match(url)
    return match.group(1) if match else None


def _primary_subtags(record: Mapping[str, Any]) -> list[str]:
    """``byLanguage`` codes reduced to their primary subtag, in record order."""
    by_language = record.get("byLanguage")
    if not isinstance(by_language, list):
        return []
    return [
        str(entry.get("code") or "").strip().split("-", 1)[0].lower()
        for entry in by_language
        if isinstance(entry, Mapping)
    ]


def authority_feed_url(payload: Any, source_id: str) -> str | None:
    """The feed the authority publishes for a mirror source id, or ``None``.

    Mirror ids and registry ids are not the same set: a multilingual authority
    is one record (``th-tmd-th``, ``tl-dnmg-en``) whose ``capAlertFeed``
    lists one URL per language, and the mirror serves each language under its
    own id (``th-tmd-en``, ``tl-dnmg-pt``). Resolution, in order:

    1. A cap-sources folder named like the id, in any record: the folder names
       *are* mirror ids (``tl-dnmg-pt`` lives in ``tl-dnmg-en``'s record).
    2. Else the record with that ``sourceId``, else the one record sharing the
       id's ``{country}-{agency}`` prefix (``th-tmd-en`` → ``th-tmd-th``).
       Two or more sharing it is ambiguous and resolves nothing.
    3. One URL: that one. Several: the URL at the index of the ``byLanguage``
       entry whose primary subtag equals the id's trailing tag (``th-tmd-en``
       → ``[en, th]`` → the first), else the first URL. Except when every URL
       is a bucket folder: then the folders are the ids, none matched in step
       1, and the id is unresolvable (``gw-inm-en`` lists ``gw-inm-fr`` and
       ``gw-inm-pt``, neither its own).

    Pure; the registry payload comes from ``_registry_payload``.
    """
    sid = source_id.strip().lower()
    if not sid:
        return None
    records = _registry_records(payload)
    for candidate in records:
        for url in _record_feed_urls(candidate):
            if _bucket_folder(url) == sid:
                return url
    record: Mapping[str, Any] | None = next(
        (r for r in records if str(r.get("sourceId")).strip() == sid), None
    )
    if record is None:
        prefix = "-".join(sid.split("-")[:2]) + "-"
        matches = [r for r in records if str(r.get("sourceId")).startswith(prefix)]
        if len(matches) != 1:
            return None
        record = matches[0]
    urls = _record_feed_urls(record)
    if not urls:
        return None
    if len(urls) == 1:
        return urls[0]
    if all(_bucket_folder(url) for url in urls):
        return None
    tag = sid.rsplit("-", 1)[-1]
    subtags = _primary_subtags(record)
    if tag in subtags and subtags.index(tag) < len(urls):
        return urls[subtags.index(tag)]
    return urls[0]


def _links_from_feed(text: str, geocode_prefixes: Sequence[str] | None) -> list[str]:
    """``_parse_feed_links`` in the provider layer's exception."""
    try:
        return _parse_feed_links(text, geocode_prefixes=geocode_prefixes)
    except ET.ParseError as err:
        raise ProviderError(f"failed to parse feed: {err}") from err


async def _fetch_feed_links(
    session: aiohttp.ClientSession,
    url: str,
    *,
    geocode_prefixes: Sequence[str] | None = None,
    user_agent: str | None = None,
) -> list[str]:
    """Fetch one feed and return its live CAP URLs; ``ProviderError`` on any failure."""
    text = await fetch_feed_text(session, url, user_agent=user_agent)
    return _links_from_feed(text, geocode_prefixes)


@dataclass(frozen=True)
class _IndexRead:
    """One read of an index feed: when, how big (in bytes), and its links."""

    read_at: datetime
    size: int
    links: tuple[str, ...]

    @property
    def heavy(self) -> bool:
        return self.size >= WMO_HEAVY_INDEX_BYTES

    def stands_in(self, now: datetime, limit: timedelta) -> bool:
        """Whether a heavy read this recent still spares the next one."""
        return self.heavy and now - self.read_at < limit


async def _feed_parses(
    session: aiohttp.ClientSession, url: str, *, user_agent: str | None = None
) -> bool:
    """Whether ``url`` answers 200 with a feed ``_parse_feed_links`` reads."""
    try:
        await _fetch_feed_links(session, url, user_agent=user_agent)
    except ProviderError:
        return False
    return True


async def async_authority_feed_available(
    session: aiohttp.ClientSession, source_id: str, *, user_agent: str | None = None
) -> bool:
    """Whether the registry resolves ``source_id`` to a feed that answers.

    The check behind pinning ``feed_source`` to ``authority`` in the options
    flow: a pin that cannot fetch would only surface later as an unavailable
    entry with a reason the user has to go and find.
    """
    payload = await _registry_payload(session, user_agent=user_agent)
    if payload is None:
        return False
    url = authority_feed_url(payload, source_id)
    if url is None:
        return False
    return await _feed_parses(session, url, user_agent=user_agent)


async def async_any_feed_available(
    session: aiohttp.ClientSession, source_id: str, *, user_agent: str | None = None
) -> bool:
    """Whether either feed ``auto`` reads answers for ``source_id``.

    The check behind switching ``feed_source`` to ``auto`` in the options
    flow, now that ``auto`` no longer always has the mirror (issue #333). The
    authority feed is asked first, as ``auto`` reads it first; the mirror
    settles it for the two sources it still serves.
    """
    if await async_authority_feed_available(session, source_id, user_agent=user_agent):
        return True
    mirror = WMO_RSS_URL.format(source_id=source_id)
    return await _feed_parses(session, mirror, user_agent=user_agent)


def wmo_feed_urls(
    options: Mapping[str, Any], source_id: str, authority_url: str | None
) -> list[tuple[str, str]]:
    """The ``(feed, url)`` pairs a poll reads, in union order.

    ``feed_source`` ``auto`` (the default, and any unrecognised value — fail
    open) yields the authority feed when one is resolved and then the mirror;
    ``mirror`` / ``authority`` pin one. The authority comes first so that an
    alert both feeds carry keeps the authority's copy, and with it a ``url``
    that outlives the mirror SWIC is retiring (issue #333). An authority URL
    that *is* the mirror (``cn-cma-xx``'s record once pointed there) is read
    once. ``authority`` with nothing resolved yields ``[]``; the caller
    decides what that means.
    """
    mirror = WMO_RSS_URL.format(source_id=source_id)
    source = options.get(CONF_FEED_SOURCE, DEFAULT_WMO_FEED_SOURCE)
    if source == FEED_AUTHORITY:
        return [(FEED_AUTHORITY, authority_url)] if authority_url else []
    feeds: list[tuple[str, str]] = []
    if source != FEED_MIRROR and authority_url and authority_url != mirror:
        feeds.append((FEED_AUTHORITY, authority_url))
    feeds.append((FEED_MIRROR, mirror))
    return feeds


def _source_languages(source: Mapping[str, Any]) -> str:
    """Join a registry record's ``byLanguage`` primary subtags, e.g. ``de/en``.

    The source-ID's trailing segment is *not* the body language and must not
    be used here: 15 of the 110 sources sampled on 2026-08-03 disagree with
    their CAP body's first ``<info>`` block (``at-zamg-en`` leads with
    ``de-DE``, ``ch-meteoswiss-de`` leads with ``en``), 17 end in ``-xx``, and
    one ends in ``-marine``. ``byLanguage`` is itself only a hint — it
    over-claims for 35 of those 110 and under-claims for 20 — so it seeds the
    display label only, never selection (see ``cap.select_info``). Registry order
    is preserved, duplicates collapsed. Returns ``""`` when absent or empty.
    """
    by_language = source.get("byLanguage")
    if not isinstance(by_language, list):
        return ""
    codes: list[str] = []
    for entry in by_language:
        if not isinstance(entry, Mapping):
            continue
        code = str(entry.get("code") or "").strip().split("-", 1)[0].lower()
        if code and code not in codes:
            codes.append(code)
    return "/".join(codes)


def _wmo_source_label(source: Mapping[str, Any]) -> str:
    """Build a compact dropdown label from a registry source record.

    Prefers ``"{countryName} ({AUTHORITYABBREV}, {langs})"``, where ``langs``
    are the record's ``byLanguage`` primary subtags (``mx-smn-es`` → ``es``,
    ``at-zamg-en`` → ``de/en``) — multi-valued labels also signal which
    sources are multilingual, and so worth setting a language option on.
    Falls back to the first ``byLanguage`` name, then the bare source ID.
    """
    sid = str(source.get("sourceId") or "").strip()
    country = str(source.get("countryName") or "").strip()
    abbrev = str(source.get("authorityAbbrev") or "").strip()
    langs = _source_languages(source)
    if country and abbrev:
        head = f"{country} ({abbrev.upper()}"
        return f"{head}, {langs})" if langs else f"{head})"
    by_language = source.get("byLanguage")
    if isinstance(by_language, list) and by_language:
        first = by_language[0]
        if isinstance(first, Mapping):
            name = str(first.get("name") or "").strip()
            if name:
                return name
    return sid


async def fetch_wmo_sources(
    session: aiohttp.ClientSession, *, user_agent: str | None = None
) -> list[tuple[str, str]]:
    """Return ``[(sourceId, label), ...]`` for every registered WMO source.

    Fetches the live SWIC registry (``WMO_SOURCES_URL``). No filtering: every
    record resolves to a feed the provider can poll (the mirror, the
    authority's own, or both under ``auto``), and a source whose feeds are
    all dead is reported by the source step's validation rather than hidden.
    Feeds also covered by MeteoAlarm or NWS are included. Returns ``[]`` on
    any failure (HTTP error, JSON error, unexpected shape); the caller falls
    back to the static ``WMO_SOURCE_NAMES`` catalog. Sorted by label.
    """
    payload = await _registry_payload(session, user_agent=user_agent)
    seen: dict[str, str] = {}
    for source in _registry_records(payload):
        sid = str(source.get("sourceId") or "").strip()
        if sid not in seen:
            seen[sid] = _wmo_source_label(source)
    return sorted(seen.items(), key=lambda item: item[1].lower())


# ---------------------------------------------------------------------------
# Alert identity
# ---------------------------------------------------------------------------


def _doc_is_live(doc: CAPDoc, now: datetime) -> bool:
    """Whether a parsed document is an ``Actual`` alert not yet expired.

    The body-level counterpart of the envelope pre-filter, and the only
    filter the bucket and national feeds get: their envelopes carry no
    ``cap:expires``, and the bucket's ``rss.xml`` is a lazily-purged recent
    list (50 items, 263 of 278 sampled bodies expired, 17 ``Test``). A
    first-sight terminal alert fires ``incident_removed`` outside boot
    (``store.py``), so an expired body must never reach the store. Fail-open
    on a missing or unparseable ``<expires>``: the body's own expiry is still
    read by normalization. Buddhist-Era years corrected as in ``_build_alert``.
    """
    if doc.status and doc.status != "Actual":
        return False
    expiries: list[datetime] = []
    for info in doc.infos:
        parsed = parse_instant(_gregorian(info.expires)) if info.expires else None
        if parsed is None:
            return True
        expiries.append(parsed)
    return not expiries or max(expiries) >= now


def _compute_wmo_id(identifier: str, fallback_url: str) -> str:
    """Hash the CAP ``<identifier>`` (or the CAP URL) to a 12-hex stable ID.

    WMO CAP identifiers are sender-scoped and stable across Update/Cancel
    re-issues for a single event, so they survive the lifecycle. Falls back
    to the CAP URL when the identifier is missing.
    """
    key = identifier or fallback_url
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# CAPAlert construction
# ---------------------------------------------------------------------------


def _build_alert(
    doc: CAPDoc,
    info: CAPInfoDoc,
    url: str,
    alert_id: str,
    alt: CAPInfoDoc | None = None,
) -> CAPAlert:
    """Build a ``CAPAlert`` from a parsed WMO CAP document.

    ``alt`` is the alternate ``<info>`` block ``select_alt_info`` chose on a
    multilingual document; its text populates the ``*_alt`` fields, matching
    what ECCC and MeteoAlarm already publish. Every other field comes from
    ``info``.
    """
    merged_params: dict[str, str] = {**info.event_codes, **info.parameters}
    points = points_from_circles(info.circles)
    return CAPAlert(
        id=alert_id,
        url=url,
        identifier=doc.identifier,
        event=info.event or info.headline,
        msg_type=doc.msg_type,
        status=doc.status,
        scope=doc.scope,
        category=info.category,
        urgency=info.urgency,
        severity=info.severity,
        certainty=info.certainty,
        response_type=",".join(info.response_type) if info.response_type else "",
        sent=_gregorian(doc.sent),
        effective=_gregorian(info.effective),
        onset=_gregorian(info.onset),
        expires=_gregorian(info.expires),
        headline=info.headline,
        description=info.description,
        instruction=info.instruction or None,
        web=info.web,
        area_desc=info.area_desc,
        geometry=geometry_from_shapes(info.polygons, points),
        points=tuple((lon, lat) for lon, lat in points),
        geocodes=geocodes_from(info.geocodes),
        sender=doc.sender,
        sender_name=info.sender_name,
        references=tuple(ref_id for _, ref_id, _ in doc.references),
        parameters=merged_params if merged_params else None,
        language=info.language,
        event_alt=(alt.event or alt.headline) if alt is not None else "",
        headline_alt=alt.headline if alt is not None else "",
        description_alt=alt.description if alt is not None else "",
        instruction_alt=(alt.instruction or None) if alt is not None else None,
        language_alt=alt.language if alt is not None else "",
        provider="wmo",
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class WMOProvider:
    """WMO SWIC per-source feed → CAP XML provider."""

    def __init__(self) -> None:
        # The authority feed, resolved from the registry on first need and
        # kept for the life of the instance (an entry reload builds a new
        # one). ``_authority_source`` is the id it was resolved for, so
        # "registry answered, no record" (``None``) is cached while "registry
        # unreachable" (nothing recorded) is retried next poll.
        self._authority_url: str | None = None
        self._authority_source: str | None = None
        # One warning per failure streak per feed, as ECCC does per host.
        self._feed_warned: dict[str, bool] = {}
        # The validators each index feed answered with, so the next poll can
        # ask for it conditionally and replay the body on a 304 (issue #304).
        self._validators = FeedValidators()
        # The last read of the authority index, keyed by its URL: a heavy one
        # is re-read on a slower cadence and this stands in between (#309).
        self._index_reads: dict[str, _IndexRead] = {}
        # Whether the current streak of failed heavy-index reads has been
        # warned about. Separate from ``_feed_warned``: a stand-in counts as
        # the feed answering, which resets that one.
        self._stand_in_warned = False
        # The mirror's current run of 404s under ``auto``, and once that
        # reaches ``WMO_MIRROR_GONE_STREAK``, when it is next probed: SWIC
        # retired its feeds (issue #333), so a 404 is no longer an outage.
        self._mirror_404s = 0
        self._mirror_probe_at: datetime | None = None

    @property
    def name(self) -> str:
        return "wmo"

    @property
    def authority_feed_url(self) -> str | None:
        """The resolved authority feed, ``None`` until the registry answered."""
        return self._authority_url

    @property
    def authority_index(self) -> dict[str, Any] | None:
        """How the authority index is being read, for diagnostics.

        ``None`` until it has been read once, and for a record that names
        the mirror itself, which is read once as the mirror.
        ``reread_interval_s`` is set when the index is heavy enough to take
        the slower cadence, and ``None`` when it is read every poll like the
        mirror.
        """
        read = self._index_reads.get(self._authority_url or "")
        if read is None:
            return None
        return {
            "bytes": read.size,
            "links": len(read.links),
            "last_read": read.read_at.isoformat(),
            "reread_interval_s": (
                int(WMO_HEAVY_INDEX_INTERVAL.total_seconds()) if read.heavy else None
            ),
        }

    async def _feed_links(
        self,
        session: aiohttp.ClientSession,
        feed: str,
        url: str,
        *,
        geocode_prefixes: Sequence[str] | None,
        user_agent: str | None,
        now: datetime,
    ) -> list[str]:
        """The live CAP URLs one feed lists this poll; ``ProviderError`` on failure.

        Both feeds are read with the validators their last ``200`` sent, so
        an unchanged index costs one request and no body (issue #304). The
        authority index is also measured: one over ``WMO_HEAVY_INDEX_BYTES``
        is re-read only every ``WMO_HEAVY_INDEX_INTERVAL``, and the links of
        its last read stand in on the polls between, and on a read that
        fails, which is retried next poll, for up to
        ``WMO_HEAVY_INDEX_STAND_IN_LIMIT`` after the read that succeeded
        (issue #309). The mirror, where it still answers, is the fresher
        feed and carries the envelope pre-filter, so it is read every poll
        regardless of size.
        """
        last = self._index_reads.get(url) if feed == FEED_AUTHORITY else None
        if last is not None and last.stands_in(now, WMO_HEAVY_INDEX_INTERVAL):
            return list(last.links)
        try:
            text = await fetch_feed_text(
                session, url, self._validators, user_agent=user_agent
            )
        except ProviderError as err:
            if last is None or not last.stands_in(now, WMO_HEAVY_INDEX_STAND_IN_LIMIT):
                raise
            if not self._stand_in_warned:
                _LOGGER.warning(
                    "WMO: %s feed failed (%s); the links of its last read stand in",
                    feed,
                    err,
                )
                self._stand_in_warned = True
            return list(last.links)
        links = _links_from_feed(text, geocode_prefixes)
        if feed == FEED_AUTHORITY:
            self._stand_in_warned = False
            read = _IndexRead(now, len(text.encode("utf-8")), tuple(links))
            if read.heavy and (last is None or not last.heavy):
                _LOGGER.info(
                    "WMO: the authority index at %s is %d KB; re-reading it every "
                    "%d min, its last link list stands in between",
                    url,
                    read.size // 1024,
                    int(WMO_HEAVY_INDEX_INTERVAL.total_seconds() // 60),
                )
            self._index_reads[url] = read
        return links

    async def _resolve_authority_url(
        self,
        session: aiohttp.ClientSession,
        source_id: str,
        *,
        user_agent: str | None = None,
    ) -> str | None:
        if self._authority_source == source_id:
            return self._authority_url
        payload = await _registry_payload(session, user_agent=user_agent)
        if payload is None:
            _LOGGER.debug(
                "WMO %s: registry unreachable; polling the mirror alone this cycle",
                source_id,
            )
            return None
        self._authority_url = authority_feed_url(payload, source_id)
        self._authority_source = source_id
        if self._authority_url is None:
            _LOGGER.debug(
                "WMO %s: no authority feed resolves for this source; mirror only",
                source_id,
            )
        return self._authority_url

    @property
    def conventions(self) -> Mapping[str, SourceConventions]:
        return _CONVENTIONS

    async def async_validate_config(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        *,
        user_agent: str | None = None,
    ) -> str | None:
        """Check that some feed actually serves this source.

        The source field accepts a custom value on purpose — the registry gains
        sources faster than the catalog here does — so a well-formed id is not
        evidence that anything is published under it. Asking the feeds is.
        The mirror answering 200 settles it. Otherwise the registry decides:
        unreachable is ``cannot_connect``; no record resolving for the id is
        ``unknown_wmo_source``; a record whose feed answers is fine (every
        source the retired mirror no longer serves, #302's Thai one first),
        and one whose feed is dead is ``cannot_connect``. Before #210 a mirror
        404 alone read as "no such source", which blamed the id for what
        turned out to be the mirror's retirement (#302, #333).
        """
        source_id = str(config.get(CONF_SOURCE_ID, "") or "").strip()
        if not source_id:
            return None
        headers = {"User-Agent": user_agent} if user_agent else None
        async with session.get(
            WMO_RSS_URL.format(source_id=source_id), headers=headers
        ) as resp:
            if resp.status == 200:
                return None
        payload = await _registry_payload(session, user_agent=user_agent)
        if payload is None:
            return "cannot_connect"
        url = authority_feed_url(payload, source_id)
        if url is None:
            return "unknown_wmo_source"
        if await _feed_parses(session, url, user_agent=user_agent):
            return None
        return "cannot_connect"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache: CAPContentCache | None = None,
        user_agent: str | None = None,
    ) -> list[CAPAlert]:
        """Fetch active alerts from a WMO SWIC source.

        (a) Resolves the authority feed from the registry (once per instance)
            and fetches each feed ``wmo_feed_urls`` names, in order.
        (b) Extracts per-item CAP XML URLs, unioned across feeds.
        (c) Fetches CAP XML for each via a shared cache (bounded concurrency).
        (d) Parses CAP XML in the thread pool executor, keeps the first copy
            of each identifier, and drops non-``Actual`` and expired bodies.
        (e) Resolves revision chains to leaf revisions.
        (f) Selects the preferred-language ``<info>`` block, builds CAPAlert
            objects, and applies the optional GPS filter.

        Per-feed failure is tolerated under ``auto``: as long as one feed
        answers the union is returned, with one warning per failure streak
        per feed. Only every feed read failing raises ``ProviderError``,
        naming each feed and its error. A mirror 404 beside another feed is
        not warned about: after ``WMO_MIRROR_GONE_STREAK`` of them the mirror
        is skipped and probed once per ``WMO_HEAVY_INDEX_INTERVAL`` until it
        answers again (issue #333).
        """
        source_id = (config.get(CONF_SOURCE_ID) or "").strip()
        if not source_id:
            raise ProviderError("WMO: source_id not configured")

        feed_source = options.get(CONF_FEED_SOURCE, DEFAULT_WMO_FEED_SOURCE)
        authority_url: str | None = None
        if feed_source != FEED_MIRROR:
            authority_url = await self._resolve_authority_url(
                session, source_id, user_agent=user_agent
            )
        feeds = wmo_feed_urls(options, source_id, authority_url)
        if not feeds:
            raise ProviderError(
                f"WMO {source_id}: feed source is pinned to the authority but no "
                "authority feed resolves for this source (registry unreachable, "
                "no record, or an ambiguous one)"
            )

        prefixes = options.get(CONF_GEOCODE_PREFIXES)
        now = _utcnow()
        cap_urls: list[str] = []
        failures: list[str] = []
        attempted = 0
        for feed, url in feeds:
            beside_another = feed == FEED_MIRROR and len(feeds) > 1
            if (
                beside_another
                and self._mirror_probe_at is not None
                and now < self._mirror_probe_at
            ):
                continue
            attempted += 1
            try:
                links = await self._feed_links(
                    session,
                    feed,
                    url,
                    geocode_prefixes=prefixes,
                    user_agent=user_agent,
                    now=now,
                )
            except ProviderError as err:
                failures.append(f"{feed} feed ({url}): {err}")
                if (
                    beside_another
                    and isinstance(err, FeedStatusError)
                    and err.status == 404
                ):
                    self._note_mirror_404(source_id, now)
                elif len(feeds) > 1 and not self._feed_warned.get(feed):
                    _LOGGER.warning(
                        "WMO %s: %s feed failed (%s); continuing with the other feed",
                        source_id,
                        feed,
                        err,
                    )
                    self._feed_warned[feed] = True
                continue
            if feed == FEED_MIRROR:
                self._mirror_404s = 0
                self._mirror_probe_at = None
            self._feed_warned[feed] = False
            cap_urls.extend(link for link in links if link not in cap_urls)

        if failures and len(failures) == attempted:
            if attempted == 1:
                raise ProviderError(f"WMO {source_id}: {failures[0]}")
            raise ProviderError(
                f"WMO {source_id}: every feed failed: " + "; ".join(failures)
            )

        if not cap_urls:
            return []

        # (c) Fetch CAP XML with bounded concurrency via the shared cache.
        cache = (
            cap_content_cache if cap_content_cache is not None else CAPContentCache()
        )
        semaphore = asyncio.Semaphore(5)

        async def _fetch_one(cap_url: str) -> str | None:
            async with semaphore:
                return await cache.get_or_fetch(session, cap_url, user_agent=user_agent)

        bodies: list[str | None] = await asyncio.gather(
            *[_fetch_one(cap_url) for cap_url in cap_urls]
        )

        # (d) Parse CAP XML in the executor (CPU-bound). Drop CAP fetch
        # failures gracefully — a missing body means that one alert is skipped.
        # An alert reached through both feeds keeps its first copy, the
        # authority's, so ``url`` does not depend on the mirror (issue #333).
        loop = asyncio.get_running_loop()
        now = _utcnow()
        parsed: list[tuple[str, CAPDoc]] = []
        seen_identifiers: set[str] = set()
        for cap_url, body in zip(cap_urls, bodies):
            if body is None:
                # The cache already warned, once per URL; this line only ties
                # the miss to the source for anyone reading at debug.
                _LOGGER.debug("WMO %s: CAP fetch failed for %s", source_id, cap_url)
                continue
            doc = await loop.run_in_executor(None, parse_cap_alert, body)
            if doc is None:
                continue
            if doc.identifier:
                if doc.identifier in seen_identifiers:
                    continue
                seen_identifiers.add(doc.identifier)
            if _doc_is_live(doc, now):
                parsed.append((cap_url, doc))

        # (e) Resolve revision chains within this poll.
        leaf_ids = {d.identifier for d in resolve_chain_leaves([d for _, d in parsed])}

        # (f) Build CAPAlert objects for the leaf revisions.
        language = str(options.get(CONF_LANGUAGE, "") or "").strip()
        alerts: list[CAPAlert] = []
        for cap_url, doc in parsed:
            if doc.identifier and doc.identifier not in leaf_ids:
                continue
            info = select_info(doc, language)
            alt = select_alt_info(doc, info)
            alert_id = _compute_wmo_id(doc.identifier, cap_url)
            alerts.append(_build_alert(doc, info, cap_url, alert_id, alt))

        gps_loc = config.get(CONF_GPS_LOC)
        if gps_loc:
            return self._filter_by_polygon(alerts, gps_loc, source_id)
        return alerts

    def _note_mirror_404(self, source_id: str, now: datetime) -> None:
        """Count a mirror 404 under ``auto``; past the streak, back it off."""
        self._mirror_404s += 1
        if self._mirror_404s < WMO_MIRROR_GONE_STREAK:
            _LOGGER.debug(
                "WMO %s: mirror feed answered 404 (%d in a row)",
                source_id,
                self._mirror_404s,
            )
            return
        if self._mirror_probe_at is None:
            _LOGGER.info(
                "WMO %s: the SWIC mirror answers 404 (SWIC retired its feeds in "
                "2026-10); probing it every %d min",
                source_id,
                int(WMO_HEAVY_INDEX_INTERVAL.total_seconds() // 60),
            )
        self._mirror_probe_at = now + WMO_HEAVY_INDEX_INTERVAL

    @staticmethod
    def _filter_by_polygon(
        alerts: list[CAPAlert], gps_loc: str, source_id: str
    ) -> list[CAPAlert]:
        """Keep alerts whose geometry contains the configured GPS point.

        Fails loud when the feed has alerts but none carry polygons — that
        signals the source does not publish per-alert geometry, so GPS mode
        cannot work (matches the ECCC/MeteoAlarm GPS-mode contract).
        """
        if not alerts:
            return []
        if not any(alert_polygons(a) for a in alerts):
            raise ProviderError(
                f"WMO {source_id}: GPS filter requested but {len(alerts)} alerts "
                "carry no polygons; this source does not publish per-alert geometry"
            )
        gps = parse_gps(gps_loc)
        if gps is None:
            raise ProviderError(f"WMO {source_id}: invalid GPS coordinates")
        lat, lon = gps
        kept: list[CAPAlert] = []
        for alert in alerts:
            for ring in alert_polygons(alert):
                if point_in_polygon(lat, lon, ring):
                    kept.append(alert)
                    break
        return kept
