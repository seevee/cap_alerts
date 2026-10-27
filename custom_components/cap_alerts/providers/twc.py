"""U.S. Tsunami Warning Centers: per-product slots → CAP bulletins (issue #298).

NTWC (Palmer, ``PAAQ``) and PTWC (Honolulu, ``PHEB``) publish no feed. The
documented ``events/xml/{PAAQ,PHEB}CAP.xml`` and the Atom files beside them
hold one message each, the center's latest across every product, so an
aftershock statement replaces a running warning sequence in them. What works
is undocumented: each product has a JSON slot, ``events/json/{slot}.json``,
holding its latest bulletin, and that slot's ``eventDir`` plus
``{TWCID}CAP.xml`` is the bulletin's CAP.

**One region, two slots.** An entry picks a domestic region
(``const.TWC_REGIONS``) and polls its warning product and its information
statement product. A slot answers ``If-Modified-Since`` with ``304``, so a
quiet poll is two requests and no transfer. A CAP body is fetched only when a
slot's ``eventDir`` moves, since a bulletin never changes at its own URL. A
slot keeps its last bulletin forever and 404s until its product is first
issued, so a 404 is an empty slot, not a failure; liveness comes from the
bulletin's own ``expires``. Slots are read with a pattern rather than as JSON
because one of them isn't JSON (``"eventLat" : 28.,`` on PHEBWEHW42).

**Identity is the product plus the incident.** ``<incidents>`` is the event
id and holds across a product's bulletins (Watch → Advisory → Cancellation).
It does not hold across products: PTWC writes ``PHEB-{n}-{eventid}`` as the
``<identifier>`` of every product for one event, and both centers send as
``ntwc@noaa.gov``, so three different statements were all
``PHEB-1-26297050`` on 2026-10-25. The store links revisions by identifier,
so ``identifier`` and ``references`` are qualified with the product
(``PHEB-1-26297050/WEHW42``). Without that, one product's ``<references>``
would supersede another product's alert, which would then leave without an
``incident_removed``.

**The end is a Cancellation bulletin**, ``msgType=Update`` with urgency Past
and a VTEC ``CAN`` action. The provider marks it in ``lifecycle_status`` and
the ``twc`` row maps that to an ending. Every bulletin carries an
``expires`` 1 to 2.5 hours out, so time bounds a bulletin the provider fails
to see, including one a second event pushes out of its slot.

**No geometry.** Warning bulletins describe the threatened coast in text and
UGC zones only. Statements and cancellations carry one ``<circle>``, at the
epicenter, which drawn as the alert's area would pin a quake thousands of
kilometres away onto the user's map. The epicenter stays in the
``EventLatLon`` parameter.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from typing import Any

import aiohttp

from ..const import CONF_PROVINCE, TWC_BASE_URL, TWC_REGIONS, TWC_SLOT_URL
from ..conventions import SourceConventions
from ..model import CAPAlert, geocodes_from
from . import ProviderError
from .cap import CAPDoc, parse_cap_alert, select_info
from .cap_content_cache import CAPContentCache
from .revalidate import FeedStatusError, FeedValidators, fetch_feed_text
from .twc_conventions import CONVENTIONS as _CONVENTIONS
from .twc_conventions import TWC_CANCELLED

_LOGGER = logging.getLogger(__name__)

_EVENT_DIR_RE = re.compile(r'"eventDir"\s*:\s*"(/events/[^"]+/)"')

_CANCELLATION_EVENT = "tsunami cancellation"


def event_dir_from_slot(text: str) -> str | None:
    """The slot's ``eventDir`` (``/events/PHEB/…/WEHW40/``), or ``None``."""
    match = _EVENT_DIR_RE.search(text)
    return match.group(1) if match else None


def compute_twc_id(slot: str, doc: CAPDoc) -> str:
    """Hash the product and the event to a 12-hex id.

    ``<incidents>`` is absent on no bulletin sampled; the identifier stands in
    if one ever is, which costs one entity per bulletin rather than a merge.
    """
    incident = doc.incidents.strip() or doc.identifier.strip()
    return hashlib.sha256(f"{slot}:{incident}".encode()).hexdigest()[:12]


def _qualify(identifier: str, product: str) -> str:
    return f"{identifier}/{product}" if identifier else identifier


def _build_alert(slot: str, doc: CAPDoc, url: str) -> CAPAlert:
    """Build a ``CAPAlert`` from one product's latest bulletin."""
    product = slot[4:]
    info = select_info(doc, "")
    parameters = dict(info.parameters)
    cancelled = (
        not doc.infos
        or info.event.strip().casefold() == _CANCELLATION_EVENT
        or ".CAN." in parameters.get("VTEC", "")
    )
    return CAPAlert(
        id=compute_twc_id(slot, doc),
        url=url,
        identifier=_qualify(doc.identifier, product),
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
        geocodes=geocodes_from(info.geocodes),
        sender=doc.sender,
        sender_name=info.sender_name,
        references=tuple(_qualify(ref_id, product) for _, ref_id, _ in doc.references),
        parameters=parameters or None,
        language=info.language,
        lifecycle_status=TWC_CANCELLED if cancelled else "",
        provider="twc",
    )


class TWCProvider:
    """One domestic region's two product slots → their latest bulletins."""

    def __init__(self) -> None:
        self._validators = FeedValidators()
        # Per slot: the ``eventDir`` last read and the bulletin parsed from it.
        self._bulletins: dict[str, tuple[str, CAPDoc]] = {}

    @property
    def name(self) -> str:
        return "twc"

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
        """Nothing to ask: the region is a closed list and its slots are fixed."""
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
        """Read the region's slots and build one alert per issued product.

        A slot that cannot be read fails the poll. A bulletin body that cannot
        be fetched or parsed skips that slot for this cycle, and the store
        retains whatever it held until the bulletin's ``expires``.
        """
        region = str(config.get(CONF_PROVINCE, "") or "").strip().upper()
        entry = TWC_REGIONS.get(region)
        if entry is None:
            raise ProviderError(f"Tsunami: unknown region {region!r}")
        _label, slots = entry

        alerts: list[CAPAlert] = []
        for slot in slots:
            bulletin = await self._async_slot_bulletin(session, slot, user_agent)
            if bulletin is not None:
                url, doc = bulletin
                alerts.append(_build_alert(slot, doc, url))
        return alerts

    async def _async_slot_bulletin(
        self, session: aiohttp.ClientSession, slot: str, user_agent: str | None
    ) -> tuple[str, CAPDoc] | None:
        """The slot's latest bulletin as ``(CAP url, doc)``, or ``None``."""
        slot_url = TWC_SLOT_URL.format(slot=slot)
        try:
            text = await fetch_feed_text(
                session, slot_url, self._validators, user_agent=user_agent
            )
        except FeedStatusError as err:
            if err.status == 404:
                return None
            raise ProviderError(f"TWC {slot}: slot HTTP {err.status}") from err
        event_dir = event_dir_from_slot(text)
        if event_dir is None:
            raise ProviderError(f"TWC {slot}: slot has no eventDir")
        cap_url = f"{TWC_BASE_URL}{event_dir}{slot[:4]}CAP.xml"

        last = self._bulletins.get(slot)
        if last is not None and last[0] == event_dir:
            return cap_url, last[1]
        try:
            body = await fetch_feed_text(session, cap_url, user_agent=user_agent)
        except ProviderError as err:
            _LOGGER.warning("TWC %s: bulletin %s unavailable: %s", slot, cap_url, err)
            return None
        doc = parse_cap_alert(body)
        if doc is None or not doc.identifier:
            _LOGGER.warning("TWC %s: bulletin %s is not CAP", slot, cap_url)
            return None
        self._bulletins[slot] = (event_dir, doc)
        return cap_url, doc
