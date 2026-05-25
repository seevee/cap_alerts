"""Environment Canada NAAD Atom feed provider."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from xml.etree.ElementTree import Element

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import CONF_GPS_LOC, CONF_LANGUAGE, CONF_PROVINCE
from ..model import CAPAlert
from .cap import CAPDoc, CAPInfoDoc, parse_cap_alert, resolve_chain_leaves
from .cap_content_cache import CAPContentCache

_LOGGER = logging.getLogger(__name__)

NAAD_FEED_URL = "https://rss.naad-adna.pelmorex.com/"

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_GEORSS = "http://www.georss.org/georss"
NS_CAP = "urn:oasis:names:tc:emergency:cap:1.2"


# ---------------------------------------------------------------------------
# Atom envelope helpers (unchanged from original)
# ---------------------------------------------------------------------------


def _parse_categories(entry: Element) -> dict[str, str]:
    """Extract category term key=value pairs from an Atom entry."""
    cats: dict[str, str] = {}
    for cat in entry.findall(f"{{{NS_ATOM}}}category"):
        term = cat.get("term", "")
        if "=" in term:
            key, _, val = term.partition("=")
            cats[key.strip()] = val.strip()
    return cats


def _parse_georss_polygon(entry: Element) -> list[list[float]] | None:
    """Parse <georss:polygon> into a list of [lon, lat] coordinate pairs."""
    poly_el = entry.find(f"{{{NS_GEORSS}}}polygon")
    if poly_el is None or not poly_el.text:
        return None
    parts = poly_el.text.strip().split()
    if len(parts) < 6 or len(parts) % 2 != 0:
        return None
    coords = []
    for i in range(0, len(parts), 2):
        try:
            lat = float(parts[i])
            lon = float(parts[i + 1])
            coords.append([lon, lat])
        except ValueError:
            return None
    return coords


def _point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Polygon is [[lon, lat], ...]."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _matches_province(area_desc: str, geocode: str, province: str) -> bool:
    """Check if an alert matches the configured province."""
    province_upper = province.upper()
    if geocode and geocode[:2].upper() == province_upper:
        return True
    if province_upper in area_desc.upper():
        return True
    return False


# ---------------------------------------------------------------------------
# CAP XML parsing
# ---------------------------------------------------------------------------


def _pick_cap_link(entry: Element) -> tuple[str, str]:
    """Return (cap_url, web_url) from Atom entry <link> elements.

    Prefers explicit MIME types; falls back to href extension heuristics.
    """
    cap_url = ""
    web_url = ""

    for link_el in entry.findall(f"{{{NS_ATOM}}}link"):
        href = link_el.get("href", "")
        link_type = link_el.get("type", "")
        if not cap_url and link_type == "application/cap+xml":
            cap_url = href
        if not web_url and link_type == "text/html":
            web_url = href

    # Fallback: extension heuristics for links without explicit type
    if not cap_url or not web_url:
        for link_el in entry.findall(f"{{{NS_ATOM}}}link"):
            href = link_el.get("href", "")
            link_type = link_el.get("type", "")
            if link_type in ("application/cap+xml", "text/html"):
                continue
            href_lower = href.lower()
            if not cap_url and (
                href_lower.endswith(".cap") or href_lower.endswith(".xml")
            ):
                cap_url = href
            elif not web_url:
                web_url = href

    return cap_url, web_url


# ---------------------------------------------------------------------------
# Info selection
# ---------------------------------------------------------------------------


def _select_info(doc: CAPDoc, language: str) -> CAPInfoDoc:
    """Pick the <info> block matching language; fall back to first."""
    if not doc.infos:
        return CAPInfoDoc()
    for info in doc.infos:
        if info.language == language:
            return info
    return doc.infos[0]


# ---------------------------------------------------------------------------
# Event name normalisation
# ---------------------------------------------------------------------------

# ECCC CAP XML uses a generic category string in <event> for some alert types
# (e.g. "weather" for Special Weather Statements).  When that happens the
# specific event name is only available in <headline>, formatted as:
#   "<Event Type> in effect [for <Area>]"
#   "<Event Type> continued"
#   "<Event Type> ended"   … etc.
_ECCC_GENERIC_EVENTS: frozenset[str] = frozenset({"weather"})

# ECCC CAP <parameter> keys carrying the canonical event name (e.g.
# "yellow warning - wind").  v1.1 preferred when both layers are present.
_ALERT_NAME_PARAM_KEYS: tuple[str, ...] = (
    "layer:EC-MSC-SMC:1.1:Alert_Name",
    "layer:EC-MSC-SMC:1.0:Alert_Name",
)

# Trailing separator chars left over after stripping a status suffix from a
# colour-coded headline like "Yellow Warning - Wind - in effect".
_TRAILING_SEPARATORS = re.compile(r"[\s\-–—:,;·]+$")


def _strip_trailing_separators(text: str) -> str:
    return _TRAILING_SEPARATORS.sub("", text)


# Status suffixes stripped from ECCC headlines to recover the bare event type.
# Ordered longest-first so " in effect for " beats " in effect".
_HEADLINE_SUFFIXES: tuple[str, ...] = (
    " in effect for ",
    " en vigueur pour ",
    " in effect",
    " en vigueur",
    " continued for ",
    " continued",
    " maintenu pour ",
    " maintenue pour ",
    " maintenu",
    " maintenue",
    " ended",
    " terminé",
    " terminée",
    " cancelled",
    " annulé",
    " annulée",
    " lifted",
    " levé",
    " levée",
    " extended for ",
    " prolongé pour ",
    " extended",
    " prolongé",
    " prolongée",
)


def _headline_to_event(headline: str) -> str:
    """Strip a status suffix from an ECCC headline to recover the event type.

    Example: "Special Weather Statement in effect for James Bay" → "Special Weather Statement"
    """
    text = headline.strip()
    lower = text.lower()
    for suffix in _HEADLINE_SUFFIXES:
        idx = lower.find(suffix)
        if idx > 0:
            return _strip_trailing_separators(text[:idx].strip())
    return text


def _best_event_name(
    event: str,
    headline: str,
    atom_title: str = "",
    parameters: Mapping[str, str] | None = None,
) -> str:
    """Return the best display name for an ECCC alert event.

    ECCC's CAP <event> is a generic category ("weather") or a lowercase event
    type ("special weather statement").  Production data is often all-lowercase
    across the Atom <title>, the CAP <headline>, and <event>.  We try sources
    in order of fidelity:
      1. CAP <parameter> ``Alert_Name`` — the provider's canonical event name.
      2. Atom <title> / CAP <headline> with status suffix stripped.
      3. Raw CAP <event>.
    Results are title-cased only when nothing properly-cased is available.
    """
    if parameters:
        for key in _ALERT_NAME_PARAM_KEYS:
            candidate = parameters.get(key, "").strip()
            candidate = _strip_trailing_separators(candidate)
            if not candidate:
                continue
            return candidate if candidate != candidate.lower() else candidate.title()

    for candidate in (atom_title, headline):
        if not candidate:
            continue
        extracted = _headline_to_event(candidate)
        if not extracted:
            continue
        if extracted != extracted.lower():
            return extracted
        return extracted.title()
    if event:
        return event if event != event.lower() else event.title()
    return event


# ---------------------------------------------------------------------------
# Alert identity
# ---------------------------------------------------------------------------


def _bilingual_key(doc: CAPDoc, info: CAPInfoDoc) -> str:
    """Compute a language-independent 12-hex ID shared by en/fr siblings.

    Uses (sender, sent, primary CAP-CP eventCode value, polygon-hash).
    Urgency is excluded by design — urgency shifts between revisions and
    caused the original dedup bug in ``_compute_eccc_id``.
    """
    primary_event_code = next((v for v in info.event_codes.values() if v), "")

    if info.polygons:
        poly_parts = [
            f"{round(lon, 6)},{round(lat, 6)}"
            for ring in info.polygons
            for lon, lat in ring
        ]
        polygon_hash = hashlib.sha256(" ".join(poly_parts).encode()).hexdigest()[:16]
    else:
        polygon_hash = ""

    if not primary_event_code and not polygon_hash:
        _LOGGER.warning(
            "ECCC: alert %s has no eventCode or polygon; bilingual pairing may fail",
            doc.identifier,
        )
        key = f"{doc.sender}|{doc.sent}|{info.area_desc}"
    else:
        key = f"{doc.sender}|{doc.sent}|{primary_event_code}|{polygon_hash}"

    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _fallback_id(atom_id: str, language: str) -> str:
    """Fallback ID for metadata-only alerts when CAP fetch fails."""
    return hashlib.sha256(f"{atom_id}|{language}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# CAPAlert construction
# ---------------------------------------------------------------------------


def _build_alert_from_cap(
    doc: CAPDoc,
    info: CAPInfoDoc,
    atom_metadata: dict[str, Any],
    fallback_web: str,
    alert_id: str,
) -> CAPAlert:
    """Build CAPAlert from CAP body fields."""
    if len(info.polygons) == 1:
        geometry: dict | None = {"type": "Polygon", "coordinates": [info.polygons[0]]}
    elif len(info.polygons) > 1:
        geometry = {
            "type": "MultiPolygon",
            "coordinates": [[ring] for ring in info.polygons],
        }
    else:
        geometry = None

    # Merge event_codes into parameters (parameters win on collision)
    merged_params: dict[str, str] = {**info.event_codes, **info.parameters}

    return CAPAlert(
        id=alert_id,
        url=atom_metadata.get("atom_id", ""),
        identifier=doc.identifier,
        event=_best_event_name(
            info.event,
            info.headline,
            atom_metadata.get("title", ""),
            info.parameters,
        ),
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
        web=info.web or fallback_web,
        area_desc=info.area_desc,
        geometry=geometry,
        geocode_same=tuple(info.geocodes.get("SAME", ())),
        sender=doc.sender,
        sender_name=info.sender_name,
        references=tuple(ref_id for _, ref_id, _ in doc.references),
        parameters=merged_params if merged_params else None,
        language=info.language or atom_metadata.get("language", ""),
        provider="eccc",
    )


def _build_fallback_alert(
    atom_metadata: dict[str, Any],
    fallback_web: str,
    alert_id: str,
) -> CAPAlert:
    """Build a metadata-only CAPAlert from Atom envelope on CAP fetch failure."""
    polygon = atom_metadata.get("polygon")
    geometry: dict | None = (
        {"type": "Polygon", "coordinates": [polygon]} if polygon else None
    )
    event_raw = atom_metadata.get("event", "")
    title_raw = atom_metadata.get("title", "")
    event = _best_event_name(event_raw, "", title_raw)
    return CAPAlert(
        id=alert_id,
        url=atom_metadata.get("atom_id", ""),
        event=event,
        msg_type=atom_metadata.get("msg_type", ""),
        status=atom_metadata.get("status", ""),
        severity=atom_metadata.get("severity", ""),
        urgency=atom_metadata.get("urgency", ""),
        certainty=atom_metadata.get("certainty", ""),
        area_desc=atom_metadata.get("area_desc", ""),
        geometry=geometry,
        web=fallback_web,
        language=atom_metadata.get("language", ""),
        provider="eccc",
    )


# ---------------------------------------------------------------------------
# Bilingual merge (unchanged logic, new key basis)
# ---------------------------------------------------------------------------


def _merge_languages(variants: list[CAPAlert], preferred_lang: str) -> CAPAlert:
    """Merge bilingual variants into one alert with primary + alt content."""
    if len(variants) == 1:
        return variants[0]

    primary = None
    alt = None
    for v in variants:
        if v.language == preferred_lang:
            primary = v
        else:
            alt = v

    if primary is None:
        primary = variants[0]
        alt = variants[1] if len(variants) > 1 else None

    if alt is None:
        return primary

    return replace(
        primary,
        headline_alt=alt.headline,
        description_alt=alt.description,
        instruction_alt=alt.instruction,
        language_alt=alt.language,
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ECCCProvider:
    """Environment Canada NAAD Atom feed provider."""

    @property
    def name(self) -> str:
        return "eccc"

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache: CAPContentCache | None = None,
        user_agent: str | None = None,
    ) -> list[CAPAlert]:
        """Fetch active alerts from ECCC NAAD feed.

        (a) Fetches the Atom feed.
        (b) Pre-filters entries by status and location against Atom envelope.
        (c) Fetches CAP XML for survivors via a shared cache.
        (d) Parses CAP XML in the thread pool executor.
        (e) Resolves revision chains to leaf revisions.
        (f) Builds CAPAlert objects, groups by bilingual key.
        (g) Merges language variants into a single bilingual alert.
        """
        preferred_lang = options.get(CONF_LANGUAGE, "en-CA")

        async with session.get(NAAD_FEED_URL) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"ECCC NAAD feed returned {resp.status}")
            text = await resp.text()

        try:
            root = ET.fromstring(text)
        except ET.ParseError as err:
            raise UpdateFailed(f"ECCC: failed to parse Atom feed: {err}") from err

        province = config.get(CONF_PROVINCE, "")
        gps_lat, gps_lon = self._parse_gps(config)

        # (b) Pre-filter entries using Atom envelope
        SurvivorTuple = tuple[Element, str, dict[str, Any], str, str]
        survivors: list[SurvivorTuple] = []

        for entry in root.findall(f"{{{NS_ATOM}}}entry"):
            cats = _parse_categories(entry)

            if cats.get("status", "") != "Actual":
                continue

            area_desc = cats.get("areaDesc", "")
            summary = entry.findtext(f"{{{NS_ATOM}}}summary", "")
            if not area_desc and summary and summary.startswith("Area:"):
                area_desc = summary[5:].strip()

            geocode = cats.get("geocode", "")
            language = cats.get("language", "en-CA")

            if province:
                if not _matches_province(area_desc, geocode, province):
                    continue
            elif gps_lat is not None and gps_lon is not None:
                coords = _parse_georss_polygon(entry)
                if coords:
                    if not _point_in_polygon(gps_lat, gps_lon, coords):
                        continue
                else:
                    continue
            else:
                return []

            cap_url, web_url = _pick_cap_link(entry)
            atom_id = entry.findtext(f"{{{NS_ATOM}}}id", "")
            atom_title = entry.findtext(f"{{{NS_ATOM}}}title", "")

            atom_metadata: dict[str, Any] = {
                "atom_id": atom_id,
                "language": language,
                "area_desc": area_desc,
                "geocode": geocode,
                "event": cats.get("event", ""),
                "title": atom_title,
                "severity": cats.get("severity", ""),
                "urgency": cats.get("urgency", ""),
                "certainty": cats.get("certainty", ""),
                "msg_type": cats.get("msgType", ""),
                "status": cats.get("status", ""),
                "polygon": _parse_georss_polygon(entry),
            }
            survivors.append((entry, language, atom_metadata, cap_url, web_url))

        if not survivors:
            return []

        # (c) Fetch CAP XML with bounded concurrency
        cache = (
            cap_content_cache if cap_content_cache is not None else CAPContentCache()
        )
        semaphore = asyncio.Semaphore(5)

        async def _fetch_one(cap_url: str) -> str | None:
            if not cap_url:
                return None
            async with semaphore:
                return await cache.get_or_fetch(session, cap_url, user_agent=user_agent)

        bodies: list[str | None] = await asyncio.gather(
            *[_fetch_one(cap_url) for _, _, _, cap_url, _ in survivors]
        )

        # (d) Parse CAP XML in executor (CPU-bound)
        loop = asyncio.get_running_loop()
        raw_docs: list[CAPDoc | None] = []
        for body in bodies:
            if body is None:
                raw_docs.append(None)
            else:
                doc = await loop.run_in_executor(None, parse_cap_alert, body)
                raw_docs.append(doc)

        # (e) Resolve revision chains within this poll
        valid_docs = [d for d in raw_docs if d is not None]
        leaf_ids = {d.identifier for d in resolve_chain_leaves(valid_docs)}

        # (f) Build CAPAlert objects
        groups: dict[str, list[CAPAlert]] = defaultdict(list)

        for (_, language, atom_metadata, _, web_url), doc in zip(survivors, raw_docs):
            if doc is not None:
                if doc.identifier not in leaf_ids:
                    continue
                info = _select_info(doc, language)
                alert_id = _bilingual_key(doc, info)
                alert = _build_alert_from_cap(
                    doc, info, atom_metadata, web_url, alert_id
                )
            else:
                atom_id = atom_metadata["atom_id"]
                alert_id = _fallback_id(atom_id, language)
                alert = _build_fallback_alert(atom_metadata, web_url, alert_id)
                _LOGGER.warning(
                    "ECCC: CAP fetch failed for atom id %s; surfacing metadata-only alert",
                    atom_id,
                )

            groups[alert.id].append(alert)

        # (g) Merge language variants
        return [
            _merge_languages(variants, preferred_lang) for variants in groups.values()
        ]

    @staticmethod
    def _parse_gps(
        config: Mapping[str, Any],
    ) -> tuple[float | None, float | None]:
        """Extract GPS coordinates from config."""
        gps_loc = config.get(CONF_GPS_LOC, "")
        if not gps_loc:
            return None, None
        try:
            parts = gps_loc.split(",")
            return float(parts[0].strip()), float(parts[1].strip())
        except (ValueError, IndexError):
            return None, None
