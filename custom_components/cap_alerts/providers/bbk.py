"""BBK / NINA provider — Germany's federal civil-protection warnings (issue #66).

BBK (Bundesamt für Bevölkerungsschutz und Katastrophenhilfe) runs the backend
behind the NINA warning app. It aggregates the civil-protection channels —
MoWaS, KATWARN, BIWAPP and the LHP flood portal (evacuations, hazmat,
drinking-water contamination, utility outages) — alongside a severity-filtered
relay of DWD weather warnings. The civil-protection half has no other Home
Assistant path, and the ``nina`` core integration's attributes go away in HA
2026.11; the weather half duplicates what MeteoAlarm Germany already carries,
and a user running both simply gets both (documented behavior, no option).

**The wire format is CAP 1.2 serialized as JSON**, not XML. The document at
``warnings/{id}.json`` has ``identifier`` / ``sender`` / ``sent`` / ``msgType``
/ ``references`` at the top level and an ``info[]`` list of language blocks,
so ``cap.cap_doc_from_json`` turns it into the same ``CAPDoc`` the XML feeds
produce and everything after that — language selection, chain resolution,
the alert builder — is the shared machinery.

Two-step fetch, WMO/GDACS-style:

* **Index.** A district (``dashboard/{ars}.json``) or the union of the five
  national channel indexes (``{channel}/mapData.json``) for GPS scopes.
  Entries whose published expiry is already past are dropped before any
  document is fetched, so a storm day does not cost a fetch per stale entry.
* **Detail + geometry.** The CAP document and, separately, its
  ``warnings/{id}.geojson`` polygons: ``area[]`` in the document carries only
  ``areaDesc`` (verified on both channels), so the GeoJSON is the only shape
  the feed publishes. Both URLs are keyed by the immutable per-revision id and
  go through the shared content cache.

Identity is ``sha256(identifier)[:12]``, one id per revision. Every revision
is a new document that names its predecessor in ``references``
(``mow.DE-SL-SLS-W038-20260904-000`` → ``…-20260901-000``; DWD mints a fresh
epoch and uuid), so within a poll ``resolve_chain_leaves`` keeps the leaf and
across polls the store's ``references``-aware supersession fires
``incident_updated`` for the hop — the same arrangement ECCC and WMO run on.

MoWaS documents publish no ``expires`` (three of three live on 2026-09-19,
none with ``onset`` either). Under the default absence policy an alert with
no expiry, no terminal vocabulary and no termination lookup ends the moment it
leaves the index, which is this feed's contract: warnung.bund.de lists live
warnings and withdraws the rest. DWD documents do carry ``expires`` and are
retained until it when the index blinks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    BBK_ARS_KREIS_DIGITS,
    BBK_ARS_LENGTH,
    BBK_CHANNELS,
    BBK_DASHBOARD_URL,
    BBK_GEOJSON_URL,
    BBK_ID_PREFIX_CHANNELS,
    BBK_MAPDATA_URL,
    BBK_WARNING_URL,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_ZONE_ID,
)
from ..model import CAPAlert, geocodes_from
from .cap import (
    CAPDoc,
    CAPInfoDoc,
    cap_doc_from_json,
    resolve_chain_leaves,
    select_alt_info,
    select_info,
)
from .cap_content_cache import CAPContentCache
from .geometry import geometry_from_polygons
from .gps import alert_polygons, parse_gps, point_in_polygon

_LOGGER = logging.getLogger(__name__)

# Concurrency for the per-warning document and geometry fetches. Documents are
# 14–40 KiB and geometries 1–8 KiB (2026-09-19), so this is bounded by
# politeness to the host rather than by payload size.
_FETCH_CONCURRENCY = 10

# The parameter the ``bbk_channel`` value is published under. Not a CAP field:
# the channel is read off the id prefix the API mints, and it is what tells a
# MoWaS evacuation from a DWD gust warning once both are ``CAPAlert``s.
CHANNEL_PARAMETER = "bbk_channel"

# MoWaS documents carry the issuing authority's name as a parameter rather
# than in ``senderName`` (which they omit), while ``sender`` is a station code
# like ``DE-SL-SLS-W038``. Read here so the entity names the authority.
_SENDER_LANGNAME_PARAMETER = "sender_langname"

_DIGITS_RE = re.compile(r"^\d+$")


# ---------------------------------------------------------------------------
# Regionalschlüssel
# ---------------------------------------------------------------------------


def normalize_ars(value: str) -> str | None:
    """Canonical Kreis-level ARS for a typed Regionalschlüssel, or ``None``.

    Accepts anything from the five Kreis digits (``09564``) to the full twelve
    (``095640000000``), digits only, and returns the twelve-digit code with
    everything below the Kreis zeroed — the only granularity the dashboard
    answers at (see ``const.BBK_DASHBOARD_URL``). A municipality code is
    therefore widened to its district rather than rejected, which is also what
    the NINA app does with the region a user picks.
    """
    cleaned = value.strip()
    if not cleaned or not _DIGITS_RE.match(cleaned):
        return None
    if len(cleaned) < BBK_ARS_KREIS_DIGITS or len(cleaned) > BBK_ARS_LENGTH:
        return None
    return cleaned[:BBK_ARS_KREIS_DIGITS].ljust(BBK_ARS_LENGTH, "0")


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IndexEntry:
    """One index row: the warning to fetch and, when published, its expiry."""

    warning_id: str
    expires: str = ""


def _parse_index(payload: Any, *, expires_key: str) -> list[_IndexEntry]:
    """Read ``[{"id": …, <expires_key>: …}, …]`` into index entries.

    The dashboard writes the expiry as ``expires`` and mapData as
    ``expiresDate``; the DWD channel publishes one on every entry and the
    civil-protection channels on none. Rows without an ``id`` are skipped.
    Anything that is not a list is treated as an empty index, since the host
    answers a district with nothing live as ``[]``.
    """
    if not isinstance(payload, list):
        return []
    entries: list[_IndexEntry] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        warning_id = row.get("id")
        if not isinstance(warning_id, str) or not warning_id.strip():
            continue
        expires = row.get(expires_key)
        entries.append(
            _IndexEntry(
                warning_id=warning_id.strip(),
                expires=expires.strip() if isinstance(expires, str) else "",
            )
        )
    return entries


def _is_expired(expires: str, now: datetime) -> bool:
    """Whether an index expiry is already past. Fails open on anything unparseable."""
    if not expires:
        return False
    try:
        expires_at = datetime.fromisoformat(expires)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _live_entries(
    entries: Sequence[_IndexEntry], now: datetime | None = None
) -> list[_IndexEntry]:
    """De-duplicate by id (the mapData union can repeat one) and drop expired rows."""
    cutoff = now or datetime.now(timezone.utc)
    seen: set[str] = set()
    live: list[_IndexEntry] = []
    for entry in entries:
        if entry.warning_id in seen or _is_expired(entry.expires, cutoff):
            continue
        seen.add(entry.warning_id)
        live.append(entry)
    return live


# ---------------------------------------------------------------------------
# Identity and channel
# ---------------------------------------------------------------------------


def compute_bbk_id(identifier: str) -> str:
    """Hash the CAP ``identifier`` to a 12-hex id, the way WMO does."""
    return hashlib.sha256(identifier.encode()).hexdigest()[:12]


def channel_for(warning_id: str) -> str:
    """The channel a warning id's prefix names, or ``""`` for an unknown prefix."""
    prefix, sep, _ = warning_id.partition(".")
    if not sep:
        return ""
    return BBK_ID_PREFIX_CHANNELS.get(prefix.lower(), "")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _rings_from_geojson(body: str) -> list[list[list[float]]]:
    """Polygon rings from a ``warnings/{id}.geojson`` FeatureCollection.

    Every feature observed is a single-ring ``Polygon`` in ``[lon, lat]``
    order with styling properties; a ``MultiPolygon`` is accepted for the
    day one appears. Returns ``[]`` for anything else, so an alert whose
    geometry endpoint answers with something unexpected ships geocode-free
    rather than failing the poll.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []
    features = parsed.get("features")
    if not isinstance(features, list):
        return []
    rings: list[list[list[float]]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        if coordinates is None:
            continue
        gtype = geometry.get("type")
        try:
            if gtype == "Polygon":
                rings.append([[float(x), float(y)] for x, y in coordinates[0]])
            elif gtype == "MultiPolygon":
                rings.extend(
                    [[float(x), float(y)] for x, y in polygon[0]]
                    for polygon in coordinates
                    if polygon
                )
        except (TypeError, ValueError, IndexError):
            continue
    return rings


# ---------------------------------------------------------------------------
# CAPAlert construction
# ---------------------------------------------------------------------------


def _build_alert(
    doc: CAPDoc,
    info: CAPInfoDoc,
    alt: CAPInfoDoc | None,
    url: str,
    rings: list[list[list[float]]],
) -> CAPAlert:
    """Build a ``CAPAlert`` from a parsed BBK document and its fetched polygons."""
    channel = channel_for(doc.identifier)
    parameters: dict[str, str] = {**info.event_codes, **info.parameters}
    if channel:
        parameters[CHANNEL_PARAMETER] = channel
    return CAPAlert(
        id=compute_bbk_id(doc.identifier),
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
        geometry=geometry_from_polygons(rings),
        geocodes=geocodes_from(info.geocodes),
        sender=doc.sender,
        sender_name=info.sender_name
        or info.parameters.get(_SENDER_LANGNAME_PARAMETER, ""),
        references=tuple(ref_id for _, ref_id, _ in doc.references),
        parameters=parameters or None,
        language=info.language,
        event_alt=(alt.event or alt.headline) if alt is not None else "",
        headline_alt=alt.headline if alt is not None else "",
        description_alt=alt.description if alt is not None else "",
        instruction_alt=(alt.instruction or None) if alt is not None else None,
        language_alt=alt.language if alt is not None else "",
        provider="bbk",
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class BBKProvider:
    """warnung.bund.de indexes → CAP-over-JSON documents → CAPAlert."""

    @property
    def name(self) -> str:
        return "bbk"

    async def async_validate_config(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        *,
        user_agent: str | None = None,
    ) -> str | None:
        """Check that the dashboard knows the configured district.

        The ARS field is free text, so a well-formed code is not evidence that
        a district exists under it. The dashboard is: it answers 404 for an
        unknown twelve-digit code and 200 (often ``[]``) for a real one, and
        it is the URL the coordinator will poll. GPS scopes select no feed and
        have nothing to check.
        """
        ars = str(config.get(CONF_ZONE_ID, "") or "").strip()
        if not ars:
            return None
        headers = {"User-Agent": user_agent} if user_agent else None
        async with session.get(
            BBK_DASHBOARD_URL.format(ars=ars), headers=headers
        ) as resp:
            if resp.status == 404:
                return "unknown_bbk_region"
            return None if resp.status == 200 else "cannot_connect"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache: CAPContentCache | None = None,
        user_agent: str | None = None,
    ) -> list[CAPAlert]:
        """Fetch active alerts for a district or, in GPS mode, the whole country.

        (a) Fetches the district dashboard, or all five channel indexes.
        (b) Drops entries already expired, de-duplicated by id.
        (c) Fetches each CAP document through the shared cache and reads it.
        (d) Resolves revision chains to leaves.
        (e) Fetches each leaf's GeoJSON, builds alerts in the configured
            language, and applies the GPS filter when the scope is a point.
        """
        headers = {"User-Agent": user_agent} if user_agent else None
        ars = str(config.get(CONF_ZONE_ID, "") or "").strip()
        if ars:
            urls = [BBK_DASHBOARD_URL.format(ars=ars)]
            expires_key = "expires"
        else:
            urls = [BBK_MAPDATA_URL.format(channel=c) for c in BBK_CHANNELS]
            expires_key = "expiresDate"

        payloads = await asyncio.gather(
            *[self._fetch_index(session, url, headers) for url in urls],
            return_exceptions=True,
        )
        entries: list[_IndexEntry] = []
        failures: list[str] = []
        for url, payload in zip(urls, payloads):
            if isinstance(payload, BaseException):
                failures.append(f"{url}: {payload}")
                continue
            entries.extend(_parse_index(payload, expires_key=expires_key))
        # One channel index failing leaves the others authoritative for their
        # own alerts; all of them failing leaves nothing to tell an outage from
        # a quiet day, and the coordinator must not read that as every alert
        # ending.
        if len(failures) == len(urls):
            raise UpdateFailed(f"BBK: no index available ({'; '.join(failures)})")
        for failure in failures:
            _LOGGER.warning(
                "BBK: index unavailable, continuing without it: %s", failure
            )

        live = _live_entries(entries)
        if not live:
            return []

        cache = (
            cap_content_cache if cap_content_cache is not None else CAPContentCache()
        )
        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _fetch_body(url: str) -> str | None:
            async with semaphore:
                return await cache.get_or_fetch(session, url, user_agent=user_agent)

        # (c) The documents.
        doc_urls = [BBK_WARNING_URL.format(warning_id=e.warning_id) for e in live]
        bodies = await asyncio.gather(*[_fetch_body(url) for url in doc_urls])
        parsed: list[tuple[_IndexEntry, str, CAPDoc]] = []
        for entry, url, body in zip(live, doc_urls, bodies):
            if body is None:
                _LOGGER.warning("BBK: document fetch failed for %s", url)
                continue
            try:
                payload = json.loads(body)
            except ValueError:
                _LOGGER.warning("BBK: document is not JSON: %s", url)
                continue
            doc = cap_doc_from_json(payload)
            if doc is None:
                _LOGGER.warning("BBK: document is not a CAP alert: %s", url)
                continue
            parsed.append((entry, url, doc))

        # (d) Leaves only. A predecessor is normally gone from the index by the
        # time its successor is listed, so this is insurance, not the rule.
        leaf_ids = {
            d.identifier for d in resolve_chain_leaves([d for _, _, d in parsed])
        }
        leaves = [(e, u, d) for e, u, d in parsed if d.identifier in leaf_ids]

        # (e) Geometry for the leaves, then the alerts.
        geo_bodies = await asyncio.gather(
            *[
                _fetch_body(BBK_GEOJSON_URL.format(warning_id=e.warning_id))
                for e, _, _ in leaves
            ]
        )
        language = str(options.get(CONF_LANGUAGE, "") or "").strip()
        alerts: list[CAPAlert] = []
        for (entry, url, doc), geo_body in zip(leaves, geo_bodies):
            if geo_body is None:
                _LOGGER.warning(
                    "BBK: geometry fetch failed for %s; alert ships without a shape",
                    entry.warning_id,
                )
            rings = _rings_from_geojson(geo_body) if geo_body is not None else []
            info = select_info(doc, language)
            alt = select_alt_info(doc, info)
            alerts.append(_build_alert(doc, info, alt, url, rings))

        gps_loc = config.get(CONF_GPS_LOC)
        if gps_loc:
            return self._filter_by_polygon(alerts, gps_loc)
        return alerts

    @staticmethod
    async def _fetch_index(
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str] | None,
    ) -> Any:
        """Fetch one index as decoded JSON, raising ``UpdateFailed`` on a non-200."""
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"HTTP {resp.status}")
            try:
                return await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError) as err:
                raise UpdateFailed(f"malformed JSON: {err}") from err

    @staticmethod
    def _filter_by_polygon(alerts: list[CAPAlert], gps_loc: str) -> list[CAPAlert]:
        """Keep alerts whose geometry contains the configured GPS point.

        Fails loud when the feed has alerts but none carry polygons, matching
        the WMO/ECCC/MeteoAlarm/GDACS GPS-mode contract: every BBK warning has
        a ``.geojson``, so that state means the geometry host is down rather
        than that this source publishes no shapes.
        """
        if not alerts:
            return []
        if not any(a.geometry for a in alerts):
            raise UpdateFailed(
                f"BBK: GPS filter requested but {len(alerts)} alerts carry no "
                "polygons; the geometry endpoint returned no usable shapes"
            )
        gps = parse_gps(gps_loc)
        if gps is None:
            raise UpdateFailed(f"BBK: invalid GPS coordinates {gps_loc!r}")
        lat, lon = gps
        kept: list[CAPAlert] = []
        for alert in alerts:
            for ring in alert_polygons(alert):
                if point_in_polygon(lat, lon, ring):
                    kept.append(alert)
                    break
        return kept
