"""WMO Severe Weather Information Centre (SWIC) provider.

Two-step fetch: pull a per-source RSS 2.0 feed, extract the per-item
``<link>`` CAP XML URLs, fetch those via the shared ``CAPContentCache``, and
parse standard CAP 1.2 XML into ``CAPAlert`` objects.

The CAP body parsing is shared with ECCC: ``_parse_cap_alert`` is
namespace-agnostic and already handles the ``urn:oasis:names:tc:emergency:cap:1.2``
namespace WMO feeds use, and the ``CAPDoc`` / ``CAPInfoDoc`` containers and
``_resolve_chain_leaves`` revision logic are reused verbatim. The "no private
import" rule is relaxed for these intra-package sibling helpers — they are
data containers and shared CAP parsing, not API-specific implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_GPS_LOC,
    CONF_SOURCE_ID,
    WMO_SOURCES_URL,
    WMO_UNMIRRORED_SOURCES,
)
from ..model import CAPAlert
from .cap_content_cache import CAPContentCache
from .eccc import (
    CAPDoc,
    CAPInfoDoc,
    _resolve_chain_leaves,
)
from .eccc import (
    _parse_cap_alert as _parse_wmo_cap_alert,
)

_LOGGER = logging.getLogger(__name__)

WMO_RSS_URL = "https://severeweather.wmo.int/v2/cap-alerts/{source_id}/rss.xml"


# ---------------------------------------------------------------------------
# RSS envelope parsing
# ---------------------------------------------------------------------------


def _item_expires(item: Any) -> datetime | None:
    """Parse an RSS item's CAP ``expires`` extension (namespace-agnostic).

    The WMO mirror enriches each ``<item>`` with CAP-namespace elements
    (``cap:expires``, ``cap:severity``, …). Returns the expiry as an aware
    ``datetime`` (naive values are assumed UTC), or ``None`` when the element
    is absent or unparseable.
    """
    for child in item:
        if child.tag.rsplit("}", 1)[-1] != "expires" or not child.text:
            continue
        try:
            dt = parsedate_to_datetime(child.text.strip())
        except (TypeError, ValueError):
            return None
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _parse_rss_links(xml_text: str, *, now: datetime | None = None) -> list[str]:
    """Extract per-item ``<link>`` CAP XML URLs for currently-active alerts.

    RSS 2.0 ``<link>`` is a plain-text element (the URL is the element text,
    unlike Atom where it is an ``href`` attribute). Items whose CAP
    ``expires`` extension is already in the past are skipped, so high-volume
    feeds (PAGASA lists ~500 items, nearly all expired) only trigger CAP-body
    fetches for live alerts — without this the cold-start cascade exceeds the
    coordinator poll timeout. Items lacking a parseable ``expires`` are kept
    (fail-open), so feeds without the extension behave as before. Raises
    ``ET.ParseError`` on malformed XML — the caller converts that to
    ``UpdateFailed``. Returns ``[]`` for a feed with no live items.
    """
    cutoff = now or datetime.now(timezone.utc)
    root = ET.fromstring(xml_text)
    links: list[str] = []
    for item in root.iter("item"):
        link = item.findtext("link")
        if not (link and link.strip()):
            continue
        expires = _item_expires(item)
        if expires is not None and expires < cutoff:
            continue
        links.append(link.strip())
    return links


# ---------------------------------------------------------------------------
# Source registry (config-flow dropdown)
# ---------------------------------------------------------------------------


def _wmo_source_label(source: Mapping[str, Any]) -> str:
    """Build a compact dropdown label from a registry source record.

    Prefers ``"{countryName} ({AUTHORITYABBREV}, {lang})"`` (the language is
    the source-ID's trailing segment, e.g. ``mx-smn-es`` → ``es``). Falls
    back to the first ``byLanguage`` name, then the bare source ID.
    """
    sid = str(source.get("sourceId") or "").strip()
    country = str(source.get("countryName") or "").strip()
    abbrev = str(source.get("authorityAbbrev") or "").strip()
    lang = sid.rsplit("-", 1)[-1] if "-" in sid else ""
    if country and abbrev:
        head = f"{country} ({abbrev.upper()}"
        return f"{head}, {lang})" if lang else f"{head})"
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
    """Return ``[(sourceId, label), ...]`` for mirror-reachable WMO sources.

    Fetches the live SWIC registry (``WMO_SOURCES_URL``) and drops only the
    known-unmirrored sources (the ~21 that 404 on the mirror). No
    cross-provider uniqueness filtering — feeds also covered by MeteoAlarm or
    NWS are included. Returns ``[]`` on any failure (HTTP error, JSON error,
    unexpected shape); the caller falls back to the static ``WMO_SOURCE_NAMES``
    catalog. Sorted by label.
    """
    headers = {"User-Agent": user_agent} if user_agent else None
    try:
        async with session.get(WMO_SOURCES_URL, headers=headers) as resp:
            if resp.status != 200:
                return []
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return []
    except aiohttp.ClientError:
        return []

    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        return []

    seen: dict[str, str] = {}
    for entry in sources:
        if not isinstance(entry, Mapping):
            continue
        source = entry.get("source")
        if not isinstance(source, Mapping):
            continue
        sid = str(source.get("sourceId") or "").strip()
        if not sid or sid in WMO_UNMIRRORED_SOURCES or sid in seen:
            continue
        seen[sid] = _wmo_source_label(source)
    return sorted(seen.items(), key=lambda item: item[1].lower())


# ---------------------------------------------------------------------------
# Alert identity
# ---------------------------------------------------------------------------


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


def _select_info(doc: CAPDoc) -> CAPInfoDoc:
    """Pick the first ``<info>`` block (WMO feeds carry one language each)."""
    return doc.infos[0] if doc.infos else CAPInfoDoc()


def _geometry_from_polygons(
    polygons: list[list[list[float]]],
) -> dict[str, Any] | None:
    """Build a GeoJSON geometry from one or more polygon rings."""
    if not polygons:
        return None
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": [polygons[0]]}
    return {"type": "MultiPolygon", "coordinates": [[ring] for ring in polygons]}


def _build_alert(doc: CAPDoc, info: CAPInfoDoc, url: str, alert_id: str) -> CAPAlert:
    """Build a ``CAPAlert`` from a parsed WMO CAP document."""
    merged_params: dict[str, str] = {**info.event_codes, **info.parameters}
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
        sent=doc.sent,
        effective=info.effective,
        onset=info.onset,
        expires=info.expires,
        headline=info.headline,
        description=info.description,
        instruction=info.instruction or None,
        web=info.web,
        area_desc=info.area_desc,
        geometry=_geometry_from_polygons(info.polygons),
        geocode_same=tuple(info.geocodes.get("SAME", ())),
        sender=doc.sender,
        sender_name=info.sender_name,
        references=tuple(ref_id for _, ref_id, _ in doc.references),
        parameters=merged_params if merged_params else None,
        language=info.language,
        provider="wmo",
    )


# ---------------------------------------------------------------------------
# GPS polygon filtering
# ---------------------------------------------------------------------------


def _parse_gps(value: str) -> tuple[float, float] | None:
    """Extract ``(lat, lon)`` from a ``"lat,lon"`` config string."""
    if not value:
        return None
    try:
        parts = value.split(",")
        return float(parts[0].strip()), float(parts[1].strip())
    except (ValueError, IndexError):
        return None


def _point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Polygon is ``[[lon, lat], ...]``."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]  # lon, lat
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _alert_polygons(alert: CAPAlert) -> list[list[list[float]]]:
    """Extract the polygon rings stored on a CAPAlert geometry."""
    geom = alert.geometry
    if not geom:
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return []
    if gtype == "Polygon":
        return [coords[0]] if coords else []
    if gtype == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class WMOProvider:
    """WMO SWIC per-source RSS → CAP XML provider."""

    @property
    def name(self) -> str:
        return "wmo"

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

        (a) Fetches the per-source RSS feed.
        (b) Extracts per-item CAP XML URLs.
        (c) Fetches CAP XML for each via a shared cache (bounded concurrency).
        (d) Parses CAP XML in the thread pool executor.
        (e) Resolves revision chains to leaf revisions.
        (f) Builds CAPAlert objects and applies the optional GPS filter.
        """
        source_id = (config.get(CONF_SOURCE_ID) or "").strip()
        if not source_id:
            raise UpdateFailed("WMO: source_id not configured")

        url = WMO_RSS_URL.format(source_id=source_id)
        async with session.get(url) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"WMO {source_id}: RSS HTTP {resp.status}")
            rss_text = await resp.text()

        try:
            cap_urls = _parse_rss_links(rss_text)
        except ET.ParseError as err:
            raise UpdateFailed(f"WMO {source_id}: failed to parse RSS: {err}") from err

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
        loop = asyncio.get_running_loop()
        parsed: list[tuple[str, CAPDoc]] = []
        for cap_url, body in zip(cap_urls, bodies):
            if body is None:
                _LOGGER.warning("WMO %s: CAP fetch failed for %s", source_id, cap_url)
                continue
            doc = await loop.run_in_executor(None, _parse_wmo_cap_alert, body)
            if doc is not None:
                parsed.append((cap_url, doc))

        # (e) Resolve revision chains within this poll.
        leaf_ids = {d.identifier for d in _resolve_chain_leaves([d for _, d in parsed])}

        # (f) Build CAPAlert objects for the leaf revisions.
        alerts: list[CAPAlert] = []
        for cap_url, doc in parsed:
            if doc.identifier and doc.identifier not in leaf_ids:
                continue
            info = _select_info(doc)
            alert_id = _compute_wmo_id(doc.identifier, cap_url)
            alerts.append(_build_alert(doc, info, cap_url, alert_id))

        gps_loc = config.get(CONF_GPS_LOC)
        if gps_loc:
            return self._filter_by_polygon(alerts, gps_loc, source_id)
        return alerts

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
        with_polygons = [a for a in alerts if a.geometry]
        if not with_polygons:
            raise UpdateFailed(
                f"WMO {source_id}: GPS filter requested but {len(alerts)} alerts "
                "carry no polygons; this source does not publish per-alert geometry"
            )
        gps = _parse_gps(gps_loc)
        if gps is None:
            raise UpdateFailed(f"WMO {source_id}: invalid GPS coordinates {gps_loc!r}")
        lat, lon = gps
        kept: list[CAPAlert] = []
        for alert in alerts:
            for ring in _alert_polygons(alert):
                if _point_in_polygon(lat, lon, ring):
                    kept.append(alert)
                    break
        return kept
