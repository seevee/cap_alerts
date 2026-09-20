"""Australian state emergency services — EDXL-wrapped CAP-AU (issue #127).

Every Australian state runs its own agency and its own feed; there is no
national aggregator. Four of them publish the same profile — an EDXL-DE
``EDXLDistribution`` envelope carrying one CAP-AU 1.0 ``<alert>`` per
``contentObject`` — so they share one provider and differ only in URL
(``const.AU_FEEDS``): NSW RFS, Queensland Fire Department, DFES Emergency WA
and TasALERT. One entry per state. Victoria publishes GeoJSON rather than CAP
and is out of scope; SA, NT and ACT publish no CAP at all.

**One fetch per poll.** The feed *is* the document: no index, no per-alert
body, no content cache. NSW is 556 KB (65 KB gzipped, which aiohttp
negotiates); the others are under 300 KB.

**The envelope is the only new parsing.** EDXL-DE is a transport wrapper like
ECCC's Atom and WMO's RSS, so unwrapping it lives here rather than in the
shared parser; each ``<alert>`` element found under ``embeddedXMLContent`` is
handed to ``cap.cap_doc_from_element``, which reads the namespace off the
element's own tag and therefore takes QLD's and WA's ``cap:``-prefixed alerts
and NSW's and TAS's default-namespace ones identically.

**Severity is the Australian Warning System tier**, not CAP ``<severity>``,
which is uniform or near-uniform on every feed. NSW, QLD and TAS write the
tier in an ``AlertLevel`` parameter; WA writes it as the headline prefix
("Bushfire Advice MONITOR CONDITIONS - …") and publishes no such parameter,
so the provider fills one in from the headline to keep the attribute surface
uniform. The mapping to CAP tiers is the ``au`` convention row.

**``expires`` is not carried.** On every feed it is a regeneration TTL rather
than an end time: NSW and TAS stamp the envelope's ``dateTimeSent`` + 24 h on
every alert (so it recedes on every poll), QLD stamps ``sent`` + 24 h, and WA
stamps ``sent`` itself — already past on arrival, which ``normalize`` would
read as expired and the store would drop as terminal on first sight. With no
expiry, no terminal vocabulary and no termination lookup, the store's
existing rule ends an alert the moment its feed withdraws it; that is the
contract of a "current incidents" feed (WA sets RSS ``ttl`` 1, NSW
regenerates every minute or two). It is also why a failed or unparseable
fetch raises rather than returning ``[]``: under that rule an empty result
says every incident ended.

**Identity** is ``sha256("{state}:{incidents}:{eventCode}")[:12]`` where
the feed publishes CAP ``<incidents>`` and ``sha256("{state}:{identifier}")``
where not. NSW and TAS re-mint ``identifier`` on every update (NSW writes
``{sent}:{incident}``; TAS a global counter, ``IDT21037-83466`` one day and
``-83572`` the next for the same storm) and keep ``<incidents>`` constant, so
the incident is what holds one entity per fire there. TAS also publishes
more than one product per incident (a Bushfire Advice and a Smoke Alert for
the same fire, issue #218), which the govshare ``eventCode`` separates. QLD
(``WARN-633``) and WA (the warning page's id) publish no ``<incidents>`` and
use the identifier.

**Location markers.** Every alert on every feed carries exactly one
``<circle>`` at a street address — radius ``0`` on NSW and WA, ``0.5`` on
QLD — and about half also carry a fire-ground polygon. Both are the same
point-marking idiom, so the provider reads circles up to
``AU_POINT_RADIUS_KM`` as points; TAS's 10 km circles describe a real area
and stay out. Polygons keep the ``geometry`` slot and points ride alongside
in ``CAPAlert.points`` (#27 option (b)), which is what the card's radius
filter consumes.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from collections.abc import Mapping
from typing import Any
from xml.etree.ElementTree import Element

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    AU_ALERT_LEVEL_ALL,
    AU_ALERT_LEVELS,
    AU_FEEDS,
    CONF_ALERT_LEVEL,
    CONF_PROVINCE,
)
from ..conventions import AU_ALERT_LEVEL_PARAMETER, au_alert_level
from ..model import CAPAlert, geocodes_from
from .cap import CAPDoc, CAPInfoDoc, cap_doc_from_element, select_info
from .cap_content_cache import CAPContentCache
from .geometry import geometry_from_shapes, points_from_circles

_LOGGER = logging.getLogger(__name__)

# A circle of this radius (km) or less is an incident marker, not an area.
# Queensland writes every marker as ``lat,lon 0.5``; NSW and WA write ``0``.
# The one real radius seen (TAS, 10 km, alongside seven polygons) stays out.
AU_POINT_RADIUS_KM = 0.5

_CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"

# NSW writes its description as a ``<br />``-separated key/value block and
# QLD's instruction is a list of HTML anchors; the parser hands both over
# with the tags intact (the XML layer only unescapes the entities). Rendered
# as-is they show literal tags on every card, so text fields are flattened.
_BREAK_RE = re.compile(r"<\s*(?:br|/p|/li|/div)\s*/?\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def html_to_text(text: str) -> str:
    """Flatten the HTML fragments the state feeds embed in CAP text fields.

    Line breaks and block closers become newlines, every other tag is dropped
    (anchor targets included — they duplicate ``web``), entities are
    unescaped, and runs of blank lines are collapsed. Text with no markup
    comes back unchanged apart from surrounding whitespace.
    """
    if not text or "<" not in text:
        return text.strip() if text else text
    flattened = _BREAK_RE.sub("\n", text)
    flattened = _TAG_RE.sub("", flattened)
    flattened = html.unescape(flattened)
    lines = [line.strip() for line in flattened.split("\n")]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def edxl_alert_elements(xml_text: str) -> list[Element] | None:
    """Every CAP ``<alert>`` element inside an EDXL-DE distribution.

    Returns ``None`` when the text is not well-formed XML, which the caller
    treats as a failed poll rather than an empty one. An envelope with no
    ``contentObject`` at all — TasALERT on a quiet day — is a genuine empty
    list. Matching is by the CAP 1.2 namespace, wherever the element sits,
    so a bare ``<alert>`` document also resolves to itself.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        _LOGGER.debug("AU: EDXL parse error: %s", exc)
        return None
    return list(root.iter(f"{{{_CAP_NS}}}alert"))


# ---------------------------------------------------------------------------
# Identity, tiers
# ---------------------------------------------------------------------------


def compute_au_id(state: str, doc: CAPDoc, info: CAPInfoDoc) -> str:
    """Hash the stable incident reference plus the product to a 12-hex id.

    ``<incidents>`` is the identity where a feed publishes it (NSW, TAS) and
    the identifier where not (QLD, WA). TasALERT publishes more than one
    product per incident — a Bushfire Advice and a Bushfire Smoke Alert for
    the same fire, both carrying its ``TFS:…`` reference (issue #218) — so
    the incident alone would fold them onto one entity and the store would
    keep whichever the feed listed last. The govshare ``eventCode``
    (``bushFire`` vs ``smoke``) tells the products apart and does not move
    across a product's re-issues. The identifier path is left alone: that is
    already one id per document.
    """
    incidents = doc.incidents.strip()
    if not incidents:
        key = f"{state}:{doc.identifier.strip()}"
    else:
        product = ",".join(sorted(v for v in info.event_codes.values() if v))
        key = f"{state}:{incidents}:{product}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def alert_level_rank(level: str) -> int:
    """Position of a tier on the Australian Warning System ladder.

    ``1`` for Advice through ``3`` for Emergency Warning; ``0`` for anything
    else, which is where the agencies' informational tiers (``Information``,
    ``Not Applicable``, ``Planned Burn``) and alerts with no tier sit.
    """
    wanted = level.strip().casefold()
    for index, label in enumerate(AU_ALERT_LEVELS, start=1):
        if label.casefold() == wanted:
            return index
    return 0


def apply_alert_level_floor(alerts: list[CAPAlert], floor: str) -> list[CAPAlert]:
    """Keep alerts at or above ``floor`` on the ladder; ``All`` keeps every one.

    Read off the same derivation the severity convention uses, so the floor
    and the entity state cannot disagree about what tier an alert is.
    """
    if not floor or floor.strip().casefold() == AU_ALERT_LEVEL_ALL.casefold():
        return alerts
    minimum = alert_level_rank(floor)
    if minimum == 0:
        _LOGGER.warning("AU: unknown minimum alert level %r; applying no floor", floor)
        return alerts
    return [
        alert
        for alert in alerts
        if alert_level_rank(au_alert_level(alert.parameters, alert.headline)) >= minimum
    ]


# ---------------------------------------------------------------------------
# CAPAlert construction
# ---------------------------------------------------------------------------


def _build_alert(state: str, doc: CAPDoc, info: CAPInfoDoc, url: str) -> CAPAlert:
    """Build a ``CAPAlert`` from one unwrapped CAP-AU document."""
    parameters: dict[str, str] = dict(info.parameters)
    level = au_alert_level(parameters, info.headline)
    if level and AU_ALERT_LEVEL_PARAMETER not in parameters:
        # WA: the tier is in the headline only. Published under the same key
        # the other three feeds use, so a consumer reads one attribute.
        parameters[AU_ALERT_LEVEL_PARAMETER] = level
    points = points_from_circles(info.circles, AU_POINT_RADIUS_KM)
    return CAPAlert(
        id=compute_au_id(state, doc, info),
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
        # Deliberately blank: a regeneration TTL, not an end time (see module
        # docstring).
        expires="",
        headline=info.headline,
        description=html_to_text(info.description),
        instruction=html_to_text(info.instruction) or None,
        web=info.web,
        area_desc=info.area_desc,
        geometry=geometry_from_shapes(info.polygons, points),
        points=tuple((lon, lat) for lon, lat in points),
        geocodes=geocodes_from(info.geocodes),
        sender=doc.sender,
        sender_name=info.sender_name,
        references=tuple(ref_id for _, ref_id, _ in doc.references),
        parameters=parameters or None,
        language=info.language,
        provider="au",
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AUProvider:
    """One state's EDXL-DE feed → CAP-AU documents → CAPAlert."""

    @property
    def name(self) -> str:
        return "au"

    async def async_validate_config(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        *,
        user_agent: str | None = None,
    ) -> str | None:
        """Nothing to ask: the state is a closed list and the feed is fixed."""
        return None

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache: CAPContentCache | None = None,
        user_agent: str | None = None,
    ) -> list[CAPAlert]:
        """Fetch the configured state's feed and build one alert per document.

        (a) One GET of the state's EDXL-DE feed.
        (b) Unwrap every CAP ``<alert>`` and build alerts.
        (c) Apply the minimum-alert-level option, if set.
        """
        state = str(config.get(CONF_PROVINCE, "") or "").strip().upper()
        feed = AU_FEEDS.get(state)
        if feed is None:
            raise UpdateFailed(f"AU: unknown state {state!r}")
        _agency, url = feed
        headers = {"User-Agent": user_agent} if user_agent else None

        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"AU {state}: feed HTTP {resp.status}")
            text = await resp.text()

        elements = edxl_alert_elements(text)
        if elements is None:
            raise UpdateFailed(f"AU {state}: feed is not well-formed XML")

        alerts: list[CAPAlert] = []
        for element in elements:
            doc = cap_doc_from_element(element)
            if not doc.identifier:
                _LOGGER.warning("AU %s: alert with no identifier skipped", state)
                continue
            info = select_info(doc, "")
            alerts.append(_build_alert(state, doc, info, url))

        floor = str(options.get(CONF_ALERT_LEVEL) or AU_ALERT_LEVEL_ALL)
        return apply_alert_level_floor(alerts, floor)
