"""GDACS (Global Disaster Alert and Coordination System) provider.

Two-step fetch of the same shape WMO uses: pull the global RSS index, then
fetch one CAP 1.2 body per event through the shared ``CAPContentCache`` and
parse it with the provider-neutral ``cap`` module. What differs is everything
above the envelope:

* GDACS is **not a weather service**. It publishes geophysical hazards
  (``category=Geo``: earthquakes, volcanoes, tsunamis) alongside
  meteorological ones (``category=Met``: cyclones, floods), which is why this
  provider exists — the model is a CAP model, not a weather model.
* Filtering happens at the **RSS stage**, before any CAP body is fetched. Each
  item carries ``gdacs:eventtype`` and ``gdacs:alertlevel``, so the event-type
  and alert-level options are applied to the index rather than to parsed
  alerts. Green earthquakes are high-volume (41 of 60 items in a sampled
  24-hour index were green wildfires alone), and a CAP-fetch cascade of that
  size would exceed the coordinator's poll timeout.
* Identity is the **event id**, not the CAP ``<identifier>`` — see
  ``_compute_gdacs_id``.

The index covers the last 24 hours. An event that stays notionally active
without being updated inside that window drops out of the feed and its entity
despawns on that poll; for the point-in-time hazards this provider is aimed at
(earthquakes above all) that is the correct lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_ALERT_LEVEL,
    CONF_GDACS_EVENT_TYPES,
    CONF_GPS_LOC,
    GDACS_ALERT_LEVELS,
    GDACS_CAP_URL,
    GDACS_RSS_URL,
)
from ..model import CAPAlert, geocodes_from
from .cap import CAPDoc, CAPInfoDoc, parse_cap_alert, resolve_chain_leaves
from .cap_content_cache import CAPContentCache
from .geometry import geometry_from_shapes, points_from_circles
from .gps import alert_polygons, parse_gps, point_in_polygon

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RSS envelope parsing
# ---------------------------------------------------------------------------


def _item_text(item: Any, name: str) -> str:
    """Read an RSS item child by local name, ignoring its namespace.

    The fields this provider needs all live in the ``gdacs:`` extension
    namespace. Matching on the local name keeps the parser working if GDACS
    ever re-declares that namespace URI, the same way WMO's ``cap:expires``
    lookup does.
    """
    for child in item:
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return ""


def _alert_level_rank(level: str) -> int | None:
    """Rank a GDACS alert level on the ascending Green/Orange/Red scale.

    ``None`` for a value outside the scale — unrankable, not lowest. An
    unrecognised label is far more likely to mean GDACS extended the scale
    than that the event is unimportant, and ranking it lowest would only fail
    open at the default floor: under a raised floor it would silently drop a
    label plausibly *above* Red for exactly the users who asked to keep the
    severe end. The caller decides what unrankable means on each side of the
    comparison.
    """
    try:
        return GDACS_ALERT_LEVELS.index(level.strip().capitalize())
    except ValueError:
        return None


def _parse_rss_entries(
    xml_text: str,
    *,
    event_types: Sequence[str] | None = None,
    min_level: str = "",
) -> list[tuple[str, str]]:
    """Extract ``(eventtype, eventid)`` for index items passing both filters.

    ``event_types`` of ``None`` (or empty) means no event-type narrowing at
    all, so a hazard code GDACS adds later still arrives; a non-empty
    selection keeps only those codes. ``min_level`` is the alert-level floor,
    empty meaning no floor. Unrankable levels fail open on both sides of that
    comparison: an item whose level is outside the known scale passes any
    floor (a label GDACS adds later must not vanish for exactly the users who
    raised the floor), and a floor outside the scale is no floor at all.

    Items missing either identity field are skipped — without both, neither
    the CAP URL nor the alert id can be built. Raises ``ET.ParseError`` on
    malformed XML; the caller converts that to ``UpdateFailed``.
    """
    wanted = {code.strip().upper() for code in event_types or () if code.strip()}
    floor = (_alert_level_rank(min_level) or 0) if min_level else 0
    root = ET.fromstring(xml_text)
    entries: list[tuple[str, str]] = []
    for item in root.iter("item"):
        event_type = _item_text(item, "eventtype").upper()
        event_id = _item_text(item, "eventid")
        if not event_type or not event_id:
            continue
        if wanted and event_type not in wanted:
            continue
        rank = _alert_level_rank(_item_text(item, "alertlevel"))
        if rank is not None and rank < floor:
            continue
        entries.append((event_type, event_id))
    return entries


def _cap_url(event_type: str, event_id: str) -> str:
    """Build the per-event CAP URL from the RSS item's identity fields."""
    return GDACS_CAP_URL.format(eventtype=event_type, eventid=event_id)


# ---------------------------------------------------------------------------
# Alert identity
# ---------------------------------------------------------------------------


def _compute_gdacs_id(event_type: str, event_id: str) -> str:
    """Hash ``"{eventtype}:{eventid}"`` to a 12-hex stable ID.

    Deliberately **not** the CAP ``<identifier>``, which WMO hashes. GDACS
    writes it as ``GDACS_<type>_<eventid>_<episodeid>`` and the trailing
    episode id increments on every re-issue — an earthquake seen twice
    publishes ``GDACS_EQ_1556861_1723786`` and then a new episode under the
    same event. Hashing that would mint a second entity per update and
    fragment the alert's lifecycle, so identity is taken from the pair that
    actually names the *event*: its type and its id, both read from the RSS
    item rather than the body.
    """
    return hashlib.sha256(f"{event_type}:{event_id}".encode()).hexdigest()[:12]


def _sent_at(doc: CAPDoc) -> datetime:
    """Parse ``<sent>`` for ordering duplicates; unparseable sorts oldest.

    Naive values are read as UTC so every key is comparable.
    """
    try:
        parsed = datetime.fromisoformat(doc.sent)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# CAPAlert construction
# ---------------------------------------------------------------------------


def _build_alert(doc: CAPDoc, info: CAPInfoDoc, url: str, alert_id: str) -> CAPAlert:
    """Build a ``CAPAlert`` from a parsed GDACS CAP document.

    No alternate-language handling: GDACS publishes a single English
    ``<info>`` block per event, with no ``<language>`` element at all.
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
        sent=doc.sent,
        effective=info.effective,
        onset=info.onset,
        expires=info.expires,
        headline=info.headline,
        description=info.description,
        instruction=info.instruction or None,
        web=info.web,
        # GDACS writes the literal string "Polygon" here; the readable
        # location ("… in Russia 08/08/2026 …") lives in the headline. Left as
        # published rather than synthesised — the feed's own text is what the
        # other providers surface too.
        area_desc=info.area_desc,
        geometry=geometry_from_shapes(info.polygons, points),
        points=tuple((lon, lat) for lon, lat in points),
        geocodes=geocodes_from(info.geocodes),
        sender=doc.sender,
        sender_name=info.sender_name,
        references=tuple(ref_id for _, ref_id, _ in doc.references),
        parameters=merged_params if merged_params else None,
        language=info.language,
        provider="gdacs",
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class GDACSProvider:
    """GDACS global RSS index → per-event CAP XML provider."""

    @property
    def name(self) -> str:
        return "gdacs"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache: CAPContentCache | None = None,
        user_agent: str | None = None,
    ) -> list[CAPAlert]:
        """Fetch active alerts from the GDACS 24-hour index.

        (a) Fetches the global RSS index.
        (b) Filters items by event type and alert level, and derives one
            ``(eventtype, eventid)`` pair per surviving item.
        (c) Fetches CAP XML for each via a shared cache (bounded concurrency).
        (d) Parses CAP XML in the thread pool executor.
        (e) Resolves revision chains to leaf revisions.
        (f) Builds CAPAlert objects, de-duplicates them by event id, and
            applies the optional GPS filter.
        """
        headers = {"User-Agent": user_agent} if user_agent else None
        async with session.get(GDACS_RSS_URL, headers=headers) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"GDACS: RSS HTTP {resp.status}")
            rss_text = await resp.text()

        try:
            entries = _parse_rss_entries(
                rss_text,
                event_types=options.get(CONF_GDACS_EVENT_TYPES),
                min_level=str(options.get(CONF_ALERT_LEVEL, "") or ""),
            )
        except ET.ParseError as err:
            raise UpdateFailed(f"GDACS: failed to parse RSS: {err}") from err

        if not entries:
            return []

        # (c) Fetch CAP XML with bounded concurrency via the shared cache.
        cache = (
            cap_content_cache if cap_content_cache is not None else CAPContentCache()
        )
        semaphore = asyncio.Semaphore(5)

        async def _fetch_one(cap_url: str) -> str | None:
            async with semaphore:
                return await cache.get_or_fetch(session, cap_url, user_agent=user_agent)

        cap_urls = [_cap_url(event_type, event_id) for event_type, event_id in entries]
        bodies: list[str | None] = await asyncio.gather(
            *[_fetch_one(cap_url) for cap_url in cap_urls]
        )

        # (d) Parse CAP XML in the executor (CPU-bound). Drop CAP fetch
        # failures gracefully — a missing body means that one alert is skipped.
        loop = asyncio.get_running_loop()
        parsed: list[tuple[tuple[str, str], str, CAPDoc]] = []
        for entry, cap_url, body in zip(entries, cap_urls, bodies):
            if body is None:
                _LOGGER.warning("GDACS: CAP fetch failed for %s", cap_url)
                continue
            doc = await loop.run_in_executor(None, parse_cap_alert, body)
            if doc is not None:
                parsed.append((entry, cap_url, doc))

        # (e) Resolve revision chains within this poll. GDACS publishes no
        # <references>, so every doc is a leaf and this is a no-op today; it
        # stays because the chain rule is the CAP contract, not a GDACS one.
        leaf_ids = {
            doc.identifier for doc in resolve_chain_leaves([doc for *_, doc in parsed])
        }

        # (f) Build alerts, keeping the most recently sent document when the
        # index lists the same event twice (two episodes of one cyclone both
        # resolve to the same cap.aspx URL and so to the same id).
        latest: dict[str, tuple[datetime, CAPAlert]] = {}
        for (event_type, event_id), cap_url, doc in parsed:
            if doc.identifier and doc.identifier not in leaf_ids:
                continue
            info = doc.infos[0] if doc.infos else CAPInfoDoc()
            alert_id = _compute_gdacs_id(event_type, event_id)
            alert = _build_alert(doc, info, cap_url, alert_id)
            sent = _sent_at(doc)
            existing = latest.get(alert_id)
            if existing is None or sent >= existing[0]:
                latest[alert_id] = (sent, alert)
        alerts = [alert for _, alert in latest.values()]

        gps_loc = config.get(CONF_GPS_LOC)
        if gps_loc:
            return self._filter_by_polygon(alerts, gps_loc)
        return alerts

    @staticmethod
    def _filter_by_polygon(alerts: list[CAPAlert], gps_loc: str) -> list[CAPAlert]:
        """Keep alerts whose geometry contains the configured GPS point.

        Fails loud when the feed has alerts but none carry polygons, matching
        the WMO/ECCC/MeteoAlarm GPS-mode contract. GDACS encodes even a point
        hazard as an area — an earthquake ships a ~100 km-radius ring
        approximated with 150-odd vertices — so in practice this mode answers
        "did it happen near me", and the guard is defensive.
        """
        if not alerts:
            return []
        if not any(a.geometry for a in alerts):
            raise UpdateFailed(
                f"GDACS: GPS filter requested but {len(alerts)} alerts carry no "
                "polygons; this feed did not publish per-alert geometry"
            )
        gps = parse_gps(gps_loc)
        if gps is None:
            raise UpdateFailed(f"GDACS: invalid GPS coordinates {gps_loc!r}")
        lat, lon = gps
        kept: list[CAPAlert] = []
        for alert in alerts:
            for ring in alert_polygons(alert):
                if point_in_polygon(lat, lon, ring):
                    kept.append(alert)
                    break
        return kept
