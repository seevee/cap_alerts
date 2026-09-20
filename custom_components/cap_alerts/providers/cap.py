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
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intermediate CAP document model
# ---------------------------------------------------------------------------


@dataclass
class CAPAreaDoc:
    """Parsed contents of a single CAP <area> block.

    ``CAPInfoDoc``'s flattened fields destroy the polygon ↔ geocode pairing
    inside one ``<info>``, and ECCC's CAM threat areas (issue #172) are
    distinguishable from the legacy zone areas only *by* that pairing — the
    freeform polygon and its ``layer:EC-MSC-SMC:DLC:*`` geocode share an
    ``<area>``. Kept alongside the flattened fields, not instead of them:
    every existing consumer reads the flat set, and CAP 1.2 §3.2.4 makes the
    union reading correct for all of them except per-area interpretation.
    """

    area_desc: str = ""
    geocodes: dict[str, list[str]] = field(default_factory=dict)
    polygons: list[list[list[float]]] = field(default_factory=list)


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
    # ``<circle>`` elements as ``(lon, lat, radius_km)``, reproduced verbatim
    # from the feed. The radius is kept even when zero so this stays a faithful
    # record of what was published; deciding that a zero-radius circle is a
    # point is interpretation, and belongs to ``providers/geometry.py``.
    circles: list[tuple[float, float, float]] = field(default_factory=list)
    # The same shapes and codes again, grouped per ``<area>`` block, for the
    # consumers that need the pairing the flat fields discard.
    areas: list[CAPAreaDoc] = field(default_factory=list)


@dataclass
class CAPDoc:
    """Parsed top-level CAP <alert> element."""

    identifier: str = ""
    sender: str = ""
    sent: str = ""
    status: str = ""
    msg_type: str = ""
    scope: str = ""
    # CAP 1.2 §3.2.1 ``<incidents>``: the sender's own incident reference,
    # verbatim. Some senders re-mint ``identifier`` on every update and keep
    # the incident constant (NSW RFS writes ``sent:incident`` as the
    # identifier), which makes this the stable identity where it exists.
    incidents: str = ""
    references: list[tuple[str, str, str]] = field(default_factory=list)
    infos: list[CAPInfoDoc] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Coordinate rings
# ---------------------------------------------------------------------------
#
# Ring *syntax* differs by wire format — CAP writes ``lat,lon`` tokens, GeoRSS
# writes a flat ``lat lon lat lon`` run — but everything after tokenizing is
# the same: flip to GeoJSON order, fail closed on a value that is not a number,
# and require enough pairs to be a ring at all. That shared part lives here so
# the validity rule exists once; drifting copies of it are what issue #85 was.
#
# It sits in this module rather than in ``geometry.py`` to preserve the
# invariant in the module docstring: this file depends on nothing else in the
# package, and providers import it rather than the reverse.


def ring_from_lat_lon_pairs(
    pairs: Iterable[tuple[str, str]],
) -> list[list[float]] | None:
    """Build ``[[lon, lat], ...]`` from ``(lat, lon)`` string pairs.

    Returns ``None`` when any value is unparseable or fewer than three pairs
    are present — three distinct vertices being the minimum for an area, and
    the closing position being ``geometry.normalize_ring``'s job.

    Faithful to the input, not to GeoJSON: rings come back exactly as
    published, including unclosed ones.
    """
    coords: list[list[float]] = []
    for lat_s, lon_s in pairs:
        try:
            coords.append([float(lon_s), float(lat_s)])
        except ValueError:
            return None
    if len(coords) < 3:
        return None
    return coords


def parse_cap_polygon_text(text: str) -> list[list[float]] | None:
    """Parse CAP polygon (``lat,lon`` pairs) into ``[[lon, lat], ...]``.

    Public because CAP polygon syntax turns up outside CAP XML: MeteoAlarm
    publishes CAP over JSON, where the polygon field is still this format.
    One parser keeps the two from drifting the way their validity checks did
    (issue #85).
    """
    if not text:
        return None
    tokens = text.strip().split()
    pairs: list[tuple[str, str]] = []
    for token in tokens:
        if "," not in token:
            return None
        lat_s, _, lon_s = token.partition(",")
        pairs.append((lat_s, lon_s))
    return ring_from_lat_lon_pairs(pairs)


def _parse_cap_circle_text(text: str) -> tuple[float, float, float] | None:
    """Parse a CAP circle into ``(lon, lat, radius_km)``.

    CAP 1.2 §3.2.4 defines the value as a WGS-84 ``lat,lon`` pair, a space, and
    a radius in kilometres. Coordinate order is flipped to GeoJSON's on the way
    out; the radius is passed through in kilometres, unconverted.
    """
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    centre, radius_s = parts
    if "," not in centre:
        return None
    lat_s, _, lon_s = centre.partition(",")
    try:
        return (float(lon_s), float(lat_s), float(radius_s))
    except ValueError:
        return None


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
        area = CAPAreaDoc()
        desc_el = area_el.find(f"{{{ns}}}areaDesc")
        if desc_el is not None and desc_el.text:
            area.area_desc = desc_el.text.strip()
            area_descs.append(area.area_desc)

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
                # across ``<area>`` blocks is one code, not two. The per-area
                # container keeps its own copy either way — a code shared by
                # two areas belongs to both of them.
                if value not in bucket:
                    bucket.append(value)
                area_bucket = area.geocodes.setdefault(name_el.text.strip(), [])
                if value not in area_bucket:
                    area_bucket.append(value)

        for poly_el in area_el.findall(f"{{{ns}}}polygon"):
            if poly_el.text:
                ring = parse_cap_polygon_text(poly_el.text.strip())
                if ring:
                    info.polygons.append(ring)
                    area.polygons.append(ring)

        # Both elements are 0..* and coequal (CAP 1.2 §3.2.4), so circles are
        # collected the same way polygons are rather than as an alternative
        # to them.
        for circle_el in area_el.findall(f"{{{ns}}}circle"):
            if circle_el.text:
                circle = _parse_cap_circle_text(circle_el.text.strip())
                if circle is not None:
                    info.circles.append(circle)

        info.areas.append(area)

    info.area_desc = ", ".join(area_descs)
    return info


def parse_cap_alert(xml_text: str) -> CAPDoc | None:
    """Parse CAP XML into a CAPDoc. Returns None on parse error."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        _LOGGER.debug("CAP XML parse error: %s", exc)
        return None
    return cap_doc_from_element(root)


def cap_doc_from_element(root: Element) -> CAPDoc:
    """Read an already-parsed CAP ``<alert>`` element into a CAPDoc.

    The entry point for envelopes that carry several alerts in one document
    (EDXL-DE ``embeddedXMLContent``, issue #127): the provider parses the
    envelope once and hands each ``<alert>`` element here, rather than
    re-serializing it to text for ``parse_cap_alert``. The namespace is read
    off the element's own tag, so a ``cap:``-prefixed alert inside a
    default-namespace envelope resolves exactly as a standalone document does.
    """
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
        incidents=_text("incidents"),
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


# ---------------------------------------------------------------------------
# Alternate-language block selection
# ---------------------------------------------------------------------------


def _primary_subtag(tag: str) -> str:
    """The BCP 47 primary language subtag, casefolded (``zh-mo`` → ``zh``)."""
    return tag.strip().casefold().split("-", 1)[0]


def alternate_info_index(languages: Iterable[str], primary_index: int) -> int | None:
    """Pick the ``<info>`` block that becomes the ``*_alt`` content (issue #154).

    The configured language selects the primary block; this selects the one
    alternate the flat ``*_alt`` fields can carry. There is no second language
    preference to match against, so the rule is the one that serves both
    consumers: ``icons.classification_event`` wants English, and a reader wants
    the language they are likeliest to have (every 3+-language document on the
    WMO and MeteoAlarm feeds carries English, swept 2026-08-21).

    1. the first block in a *different* language from the primary whose
       primary subtag is ``en``;
    2. else the first block in a different language, in document order — on a
       two-language document that is always "the other one", and a document
       with no English at all still yields something rather than nothing;
    3. else ``None``.

    "Different language" compares primary subtags, so a block that repeats the
    primary's language never qualifies: ``ca-msc-xx`` publishes one block per
    area group (``en-CA``/``fr-CA``/``en-CA``/``fr-CA``), where preferring
    "an English block" would hand an English primary an English twin, and
    ``rs-hidmet-sr`` publishes ``sr`` and ``sr-Latn``, the same language in two
    scripts. Blocks without a ``<language>`` compare equal to each other, so a
    document of untagged blocks has no alternate.
    """
    tags = [_primary_subtag(tag) for tag in languages]
    primary_lang = tags[primary_index]
    candidates = [
        idx
        for idx, tag in enumerate(tags)
        if idx != primary_index and tag != primary_lang
    ]
    for idx in candidates:
        if tags[idx] == "en":
            return idx
    return candidates[0] if candidates else None


def language_matches(info_lang: str, preferred: str) -> bool:
    """Check language match with BCP 47 primary-subtag fallback.

    Casefolded exact match wins (``EN-us`` == ``en-US``); failing that the
    primary subtag (before the first ``-``) is compared, so ``zh-Hans``
    matches ``zh-CN`` and a bare ``en`` matches ``en-GB``. An empty tag on
    either side never matches.

    The primary-subtag step is script-blind: a ``zh-Hans`` (Simplified)
    preference matches a ``zh-HK``/``zh-mo`` (Traditional) block. That is
    deliberate — a user only reaches those sources by choosing them
    explicitly, and the related script beats an unrelated language. The same
    step is what lets a ``de-LS`` (easy-read German) preference land on plain
    ``de`` where a BBK document publishes no easy-read block.
    """
    if not info_lang or not preferred:
        return False
    info_norm = info_lang.strip().casefold()
    pref_norm = preferred.strip().casefold()
    if not info_norm or not pref_norm:
        return False
    if info_norm == pref_norm:
        return True
    return info_norm.split("-", 1)[0] == pref_norm.split("-", 1)[0]


def select_info(doc: CAPDoc, language: str) -> CAPInfoDoc:
    """Pick the ``<info>`` block matching ``language``.

    Multilingual bodies do not put the languages in a predictable order: of
    the 110 WMO sources sampled on 2026-08-03, 46 carried more than one
    ``<info>`` block and 25 of those led with a non-English one
    (``at-zamg-en`` leads with ``de-DE``), and BBK's MoWaS documents lead with
    ``de`` then ``de-LS`` before any translation.

    Preference order:
    1. first block whose tag equals ``language`` (casefolded), anywhere in the
       document, then the first whose *primary subtag* matches
       (``language_matches``). Two passes rather than one, because MoWaS
       leads with ``de`` and follows with ``de-LS``: a single pass would hand
       an easy-read preference the plain-German block it walks past first;
    2. first block whose primary subtag is ``en`` — a predictable fallback
       when the document lacks the preferred language, rather than an
       arbitrary one (a German user on ``mo-smg-xx`` gets ``en-US``, not
       ``zh-mo``);
    3. ``infos[0]``, so single-language documents, documents whose blocks
       declare no ``<language>``, and an unset language option all behave
       exactly as before.

    First match wins on duplicate tags. ``ca-aema-xx`` emits one ``<info>``
    per *area group* (``en-CA``/``fr-CA``/``en-CA``/``fr-CA``), so only its
    first group survives — the same pre-existing limitation ``infos[0]`` had,
    and the same defect class as ECCC issue #45.
    """
    if not doc.infos:
        return CAPInfoDoc()
    if language:
        wanted = language.strip().casefold()
        for info in doc.infos:
            if info.language.strip().casefold() == wanted:
                return info
        for info in doc.infos:
            if language_matches(info.language, language):
                return info
    for info in doc.infos:
        if _primary_subtag(info.language) == "en":
            return info
    return doc.infos[0]


def select_alt_info(doc: CAPDoc, primary: CAPInfoDoc) -> CAPInfoDoc | None:
    """Return the ``<info>`` block carried as the alternate, if any.

    The rule is ``alternate_info_index``: an English block in a language other
    than the primary's, else the first other-language block in document order
    (issue #154). On ``mo-smg-xx`` (``zh-mo``/``pt-PT``/``en-US``) a Chinese
    reader therefore gets English as the alternate, not Portuguese.

    A document with no ``<info>`` at all (CAP 1.2 allows it; a bare ``Cancel``
    is the live shape) has ``select_info`` hand back a blank block that is in
    nobody's list, so the lookup takes a default rather than raising.
    """
    primary_index = next(
        (i for i, info in enumerate(doc.infos) if info is primary), None
    )
    if primary_index is None:
        return None
    idx = alternate_info_index((info.language for info in doc.infos), primary_index)
    return doc.infos[idx] if idx is not None else None


# ---------------------------------------------------------------------------
# CAP serialized as JSON
# ---------------------------------------------------------------------------
#
# BBK (issue #66) publishes CAP 1.2 with the XML element names as JSON keys:
# ``identifier`` … ``references`` at the top level, ``info`` as a list of
# blocks, ``eventCode`` / ``parameter`` / ``geocode`` as lists of
# ``{"valueName", "value"}`` pairs, and ``area`` as a list. Fields that are
# 0..* in the schema (``category``, ``responseType``) arrive as lists; a
# serializer that writes a single string for them is accepted too. The reader
# yields the same ``CAPDoc`` the XML parser does, so everything downstream —
# language selection, chain resolution, the alert builder — is shared.


def _json_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _json_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [
            item.strip() for item in value if isinstance(item, str) and item.strip()
        ]
    return []


def _json_pairs(value: Any) -> list[tuple[str, str]]:
    """``[{"valueName": n, "value": v}, …]`` → ``[(n, v), …]``, blanks dropped."""
    pairs: list[tuple[str, str]] = []
    if not isinstance(value, list):
        return pairs
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _json_text(item.get("valueName"))
        val = item.get("value")
        text = (
            val.strip() if isinstance(val, str) else str(val) if val is not None else ""
        )
        if name and text:
            pairs.append((name, text))
    return pairs


def _info_from_json(block: dict[str, Any]) -> CAPInfoDoc:
    info = CAPInfoDoc(
        language=_json_text(block.get("language")),
        category=", ".join(_json_texts(block.get("category"))),
        event=_json_text(block.get("event")),
        response_type=_json_texts(block.get("responseType")),
        urgency=_json_text(block.get("urgency")),
        severity=_json_text(block.get("severity")),
        certainty=_json_text(block.get("certainty")),
        effective=_json_text(block.get("effective")),
        onset=_json_text(block.get("onset")),
        expires=_json_text(block.get("expires")),
        sender_name=_json_text(block.get("senderName")),
        headline=_json_text(block.get("headline")),
        description=_json_text(block.get("description")),
        instruction=_json_text(block.get("instruction")),
        web=_json_text(block.get("web")),
    )
    for name, value in _json_pairs(block.get("eventCode")):
        info.event_codes[name] = value
    for name, value in _json_pairs(block.get("parameter")):
        info.parameters[name] = value

    area_descs: list[str] = []
    areas = block.get("area")
    for area_block in areas if isinstance(areas, list) else []:
        if not isinstance(area_block, dict):
            continue
        area = CAPAreaDoc(area_desc=_json_text(area_block.get("areaDesc")))
        if area.area_desc:
            area_descs.append(area.area_desc)
        for name, value in _json_pairs(area_block.get("geocode")):
            bucket = info.geocodes.setdefault(name, [])
            if value not in bucket:
                bucket.append(value)
            area_bucket = area.geocodes.setdefault(name, [])
            if value not in area_bucket:
                area_bucket.append(value)
        for text in _json_texts(area_block.get("polygon")):
            ring = parse_cap_polygon_text(text)
            if ring:
                info.polygons.append(ring)
                area.polygons.append(ring)
        for text in _json_texts(area_block.get("circle")):
            circle = _parse_cap_circle_text(text)
            if circle is not None:
                info.circles.append(circle)
        info.areas.append(area)

    info.area_desc = ", ".join(area_descs)
    return info


def cap_doc_from_json(payload: Any) -> CAPDoc | None:
    """Build a ``CAPDoc`` from a CAP 1.2 document serialized as JSON.

    ``payload`` is the already-decoded object. Returns ``None`` when it is not
    a JSON object or carries no ``identifier`` — the same "not a CAP document"
    answer ``parse_cap_alert`` gives for unparseable XML — so a host that
    answers a missing warning with some other JSON body is a skipped alert,
    not a crash.
    """
    if not isinstance(payload, dict):
        return None
    identifier = _json_text(payload.get("identifier"))
    if not identifier:
        return None
    doc = CAPDoc(
        identifier=identifier,
        sender=_json_text(payload.get("sender")),
        sent=_json_text(payload.get("sent")),
        status=_json_text(payload.get("status")),
        msg_type=_json_text(payload.get("msgType")),
        scope=_json_text(payload.get("scope")),
        incidents=" ".join(_json_texts(payload.get("incidents"))),
    )
    doc.references = _parse_references(" ".join(_json_texts(payload.get("references"))))
    infos = payload.get("info")
    for block in infos if isinstance(infos, list) else []:
        if isinstance(block, dict):
            doc.infos.append(_info_from_json(block))
    return doc
