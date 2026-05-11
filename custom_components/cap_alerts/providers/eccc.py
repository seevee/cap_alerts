"""Environment Canada NAAD Atom feed provider."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from xml.etree.ElementTree import Element

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import CONF_GPS_LOC, CONF_LANGUAGE, CONF_PROVINCE
from ..model import CAPAlert
from .cap_content_cache import CAPContentCache

_LOGGER = logging.getLogger(__name__)

NAAD_FEED_URL = "https://rss.naad-adna.pelmorex.com/"

NS_ATOM = "http://www.w3.org/2005/Atom"
NS_GEORSS = "http://www.georss.org/georss"
NS_CAP = "urn:oasis:names:tc:emergency:cap:1.2"


# ---------------------------------------------------------------------------
# Intermediate CAP document model (private to this module)
# ---------------------------------------------------------------------------


@dataclass
class CAPInfoDoc:
    """Parsed contents of a single CAP <info> block."""

    language: str = ""
    category: str = ""
    event: str = ""
    response_type: list[str] = field(default_factory=list)
    urgency: str = ""
    severity: str = ""
    certainty: str = ""
    effective: str = ""
    onset: str = ""
    expires: str = ""
    sender_name: str = ""
    headline: str = ""
    description: str = ""
    instruction: str = ""
    web: str = ""
    event_codes: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, str] = field(default_factory=dict)
    area_desc: str = ""
    geocodes: dict[str, list[str]] = field(default_factory=dict)
    polygons: list[list[list[float]]] = field(default_factory=list)


@dataclass
class CAPDoc:
    """Parsed top-level CAP <alert> element."""

    identifier: str = ""
    sender: str = ""
    sent: str = ""
    status: str = ""
    msg_type: str = ""
    scope: str = ""
    references: list[tuple[str, str, str]] = field(default_factory=list)
    infos: list[CAPInfoDoc] = field(default_factory=list)


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


def _parse_cap_polygon_text(text: str) -> list[list[float]] | None:
    """Parse CAP polygon (``lat,lon`` pairs) into ``[[lon, lat], ...]``."""
    if not text:
        return None
    pairs = text.strip().split()
    if len(pairs) < 3:
        return None
    coords: list[list[float]] = []
    for pair in pairs:
        if "," not in pair:
            return None
        lat_s, _, lon_s = pair.partition(",")
        try:
            coords.append([float(lon_s), float(lat_s)])
        except ValueError:
            return None
    return coords


def _parse_references(refs_text: str) -> list[tuple[str, str, str]]:
    """Parse CAP <references> string into (sender, identifier, sent) triples."""
    refs: list[tuple[str, str, str]] = []
    if not refs_text:
        return refs
    for token in refs_text.split():
        parts = token.split(",")
        if len(parts) < 3:
            continue
        sender = parts[0]
        sent = parts[-1]
        identifier = ",".join(parts[1:-1])
        refs.append((sender, identifier, sent))
    return refs


def _parse_info(info_el: Element, ns: str) -> CAPInfoDoc:
    """Parse a single CAP <info> element into a CAPInfoDoc."""

    def _text(tag: str) -> str:
        el = info_el.find(f"{{{ns}}}{tag}")
        return el.text.strip() if el is not None and el.text else ""

    info = CAPInfoDoc(
        language=_text("language"),
        category=_text("category"),
        event=_text("event"),
        urgency=_text("urgency"),
        severity=_text("severity"),
        certainty=_text("certainty"),
        effective=_text("effective"),
        onset=_text("onset"),
        expires=_text("expires"),
        sender_name=_text("senderName"),
        headline=_text("headline"),
        description=_text("description"),
        instruction=_text("instruction"),
        web=_text("web"),
    )

    info.response_type = [
        el.text.strip() for el in info_el.findall(f"{{{ns}}}responseType") if el.text
    ]

    for ec_el in info_el.findall(f"{{{ns}}}eventCode"):
        name_el = ec_el.find(f"{{{ns}}}valueName")
        val_el = ec_el.find(f"{{{ns}}}value")
        if name_el is not None and name_el.text and val_el is not None and val_el.text:
            info.event_codes[name_el.text.strip()] = val_el.text.strip()

    for param_el in info_el.findall(f"{{{ns}}}parameter"):
        name_el = param_el.find(f"{{{ns}}}valueName")
        val_el = param_el.find(f"{{{ns}}}value")
        if name_el is not None and name_el.text and val_el is not None and val_el.text:
            info.parameters[name_el.text.strip()] = val_el.text.strip()

    area_descs: list[str] = []
    for area_el in info_el.findall(f"{{{ns}}}area"):
        desc_el = area_el.find(f"{{{ns}}}areaDesc")
        if desc_el is not None and desc_el.text:
            area_descs.append(desc_el.text.strip())

        for gc_el in area_el.findall(f"{{{ns}}}geocode"):
            name_el = gc_el.find(f"{{{ns}}}valueName")
            val_el = gc_el.find(f"{{{ns}}}value")
            if (
                name_el is not None
                and name_el.text
                and val_el is not None
                and val_el.text
            ):
                info.geocodes.setdefault(name_el.text.strip(), []).append(
                    val_el.text.strip()
                )

        for poly_el in area_el.findall(f"{{{ns}}}polygon"):
            if poly_el.text:
                ring = _parse_cap_polygon_text(poly_el.text.strip())
                if ring:
                    info.polygons.append(ring)

    info.area_desc = ", ".join(area_descs)
    return info


def _parse_cap_alert(xml_text: str) -> CAPDoc | None:
    """Parse CAP XML into a CAPDoc. Returns None on parse error."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        _LOGGER.debug("CAP XML parse error: %s", exc)
        return None

    # Detect namespace from root tag
    root_tag = root.tag
    if root_tag.startswith("{"):
        ns = root_tag[1:].partition("}")[0]
    else:
        ns = ""

    def _text(tag: str) -> str:
        prefix = f"{{{ns}}}" if ns else ""
        el = root.find(f"{prefix}{tag}")
        return el.text.strip() if el is not None and el.text else ""

    doc = CAPDoc(
        identifier=_text("identifier"),
        sender=_text("sender"),
        sent=_text("sent"),
        status=_text("status"),
        msg_type=_text("msgType"),
        scope=_text("scope"),
    )
    doc.references = _parse_references(_text("references"))

    ns_prefix = f"{{{ns}}}" if ns else ""
    for info_el in root.findall(f"{ns_prefix}info"):
        doc.infos.append(_parse_info(info_el, ns))

    return doc


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
            return text[:idx].strip()
    return text


def _best_event_name(event: str, headline: str, atom_title: str = "") -> str:
    """Return the best display name for an ECCC alert event.

    ECCC's CAP <event> is a generic category ("weather") or a lowercase event
    type ("special weather statement").  Production data is often all-lowercase
    across the Atom <title>, the CAP <headline>, and <event>.  We try sources
    in order of fidelity (Atom title → CAP headline → CAP event), strip any
    status suffix, and title-case the result when nothing properly-cased is
    available.
    """
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
# Lifecycle (revision chain resolution)
# ---------------------------------------------------------------------------


def _resolve_chain_leaves(docs: list[CAPDoc]) -> list[CAPDoc]:
    """Return docs not referenced by any other doc in the list.

    Drops superseded revisions within a single poll.  If the resulting
    leaf set is empty (all docs reference each other — shouldn't happen
    with valid CAP), returns the full list as a safe fallback.
    """
    referenced = {ref_id for doc in docs for _, ref_id, _ in doc.references}
    leaves = [doc for doc in docs if doc.identifier not in referenced]
    return leaves if leaves else docs


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
            info.event, info.headline, atom_metadata.get("title", "")
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
                doc = await loop.run_in_executor(None, _parse_cap_alert, body)
                raw_docs.append(doc)

        # (e) Resolve revision chains within this poll
        valid_docs = [d for d in raw_docs if d is not None]
        leaf_ids = {d.identifier for d in _resolve_chain_leaves(valid_docs)}

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
