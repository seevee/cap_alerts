"""Shared CAP 1.2 XML parsing — provider-neutral.

Both the ECCC (Atom-wrapped) and WMO SWIC (RSS-wrapped) providers carry
standard CAP 1.2 ``<alert>`` documents inside different envelopes. This module
parses the CAP body itself into provider-agnostic ``CAPDoc`` / ``CAPInfoDoc``
containers, independent of how the document was delivered. Parsing is
namespace-agnostic: the namespace is detected from the root tag, so the
``urn:oasis:names:tc:emergency:cap:1.2`` namespace both feeds use is handled
without per-provider configuration.

This module deliberately depends on nothing else in the package — providers
import the parser, never the reverse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intermediate CAP document model
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
# CAP XML parsing
# ---------------------------------------------------------------------------


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
                bucket = info.geocodes.setdefault(name_el.text.strip(), [])
                value = val_el.text.strip()
                # De-duplicate per scheme, order-preserving: a value repeated
                # across ``<area>`` blocks is one code, not two.
                if value not in bucket:
                    bucket.append(value)

        for poly_el in area_el.findall(f"{{{ns}}}polygon"):
            if poly_el.text:
                ring = _parse_cap_polygon_text(poly_el.text.strip())
                if ring:
                    info.polygons.append(ring)

    info.area_desc = ", ".join(area_descs)
    return info


def parse_cap_alert(xml_text: str) -> CAPDoc | None:
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
# Lifecycle (revision chain resolution)
# ---------------------------------------------------------------------------


def resolve_chain_leaves(docs: list[CAPDoc]) -> list[CAPDoc]:
    """Return docs not referenced by any other doc in the list.

    Drops superseded revisions within a single poll.  If the resulting
    leaf set is empty (all docs reference each other — shouldn't happen
    with valid CAP), returns the full list as a safe fallback.
    """
    referenced = {ref_id for doc in docs for _, ref_id, _ in doc.references}
    leaves = [doc for doc in docs if doc.identifier not in referenced]
    return leaves if leaves else docs
