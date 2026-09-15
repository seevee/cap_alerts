"""GDACS (Global Disaster Alert and Coordination System) provider.

GDACS is **not a weather service**. It publishes geophysical hazards
(earthquakes, volcanoes, tsunamis) alongside meteorological and hydrological
ones (cyclones, floods, droughts, wildfires), which is why this provider
exists — the model is a CAP model, not a weather model.

Unlike every other provider here, no CAP document is ever fetched. GDACS has no
per-event CAP endpoint that works (probed 2026-08-08):

* ``cap.aspx`` reads only its ``eventtype`` parameter. Four different
  ``eventid`` values of one type all returned the same body, so every
  earthquake in a poll resolved to whichever earthquake was newest — distinct
  ids carrying identical content, one entity per index item, all of them
  wrong. Adding ``episodeid`` does not change it.
* The per-event path each RSS item advertises in ``<gdacs:cap>`` is real but
  partial: cyclones have one, and every earthquake and flood answered HTTP 200
  with the GDACS "Admin section" HTML page. There is no 404 to detect.

So the RSS item *is* the record. Every item carries the same 41 fields across
all six hazard types, including a per-episode identity, the alert level, and
the timestamps — enough for a ``CAPAlert``, with per-event geometry fetched
separately from the GeoJSON the item's episode names. That makes GDACS the
second provider (after NWS, which reads GeoJSON) to build alerts without a CAP
parser; ``CAPAlert`` is the target shape, not the wire format.

Two consequences worth stating plainly:

**Nothing here ever gets an ``expires``.** ``<gdacs:todate>`` looks like one and
is not: it was in the past for all 315 events in a sampled current-events feed,
including all three live cyclones and all 280 live wildfires. It is the last
observation time. Mapping it to ``expires`` would mark every GDACS alert
terminal on arrival, so it travels in ``parameters`` instead.

**Withdrawal from the feed is the only end-of-life signal.** ``iscurrent`` goes
false for droughts and nothing else — every earthquake, volcano, cyclone, flood
and wildfire observed was ``iscurrent=true`` right up to the poll where it
disappeared. With no ``expires``, no terminal vocabulary and no termination
lookup, ``store._retain_on_absence`` already ends these alerts the moment they
go missing, which is the correct reading of this feed. Retention therefore
scales with significance: a major earthquake holds an entity for about four
days, a small one about a day, a drought for a year. See ``const`` for the two
indexes that produce that and why both are polled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
from defusedxml import ElementTree as ET

from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_ALERT_LEVEL,
    CONF_GDACS_EVENT_TYPES,
    CONF_GPS_LOC,
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
    GDACS_ALERT_LEVELS,
    GDACS_CAP_CLASSIFICATION,
    GDACS_DEFAULT_ALERT_LEVEL,
    GDACS_EVENT_TYPES,
    GDACS_GEOJSON_URL,
    GDACS_RSS_24H_URL,
    GDACS_RSS_CURRENT_URL,
)
from ..model import CAPAlert
from .cap_content_cache import CAPContentCache
from .geometry import geometry_from_shapes
from .gps import alert_polygons, parse_gps, point_in_polygon

_LOGGER = logging.getLogger(__name__)

# Envelope values GDACS itself publishes, identical on every CAP body sampled
# across all seven hazard types. Constants rather than parsed fields now that
# no body is fetched, so the alerts carry the same envelope they always did.
_SENDER = "info-gdacs@gdacs.org"
_SENDER_NAME = "Global Disaster Alert and Coordination System"
_STATUS = "Actual"
_SCOPE = "Public"
_URGENCY = "Past"

# GDACS publishes ``msgType=Alert`` on every body regardless of how many times
# an event has been re-issued, so a revision is visible through ``sent`` and
# the episode id rather than through the phase. Kept as published: inventing an
# ``Update`` here would be a claim the feed never makes.
_MSG_TYPE = "Alert"

# GDACS alert level → CAP severity. The feed's own bodies are no guide — they
# carry a near-constant severity per hazard type (every wildfire "Severe",
# every flood "Moderate") that tracks the hazard rather than the event — so the
# alert level, which is GDACS's actual per-event impact judgement, is what the
# entity state derives from.
_ALERT_LEVEL_SEVERITY: Mapping[str, str] = {
    "Green": "Minor",
    "Orange": "Severe",
    "Red": "Extreme",
}

# GeoJSON feature classes describing where a hazard is *forecast* to go, not
# where it is. A cyclone ships its track, its forecast cone, and 3 wind-radii
# rings per 12-hour step out to four days — some 40 rings, and a
# point-in-polygon test against them answers "might this reach me eventually"
# rather than "am I in the affected area". Excluded by prefix so an unknown
# class is *kept*: over-coverage is recoverable, a silently empty geometry is
# not.
_FORECAST_CLASS_PREFIXES = ("Poly_Cones", "Poly_WindRadii", "Poly_Polygon_Point")

# The one class the keep-unknown rule doesn't survive (#195). Every flood file
# ships two area layers: ``Poly_Affected`` is the footprint, ``Poly_Global`` is
# the outline of the affected *country*, with the footprint as one of its
# rings. Merging both makes a flood in Connecticut span Hawaii to Maine: the
# bbox zooms the card out to the whole country and GPS mode matches anyone in
# it. So the country outline is dropped whenever the footprint is present, and
# kept only when it is the sole area layer — coarse beats empty. Measured on
# all 15 live FL events 2026-09-06: both layers always present, 10 differ.
_AFFECTED_CLASS = "Poly_Affected"
_COUNTRY_OUTLINE_CLASS = "Poly_Global"

# Per-file ceiling on a geometry fetch, in place of the content cache's 10 s
# default that a CAP XML body gets (#196). GDACS geometry is not a CAP body:
# the seven events at the default Orange floor weighed 4.8 MB on 2026-09-06,
# the largest 2.4 MB, and gdacs.org served the same file in 5 s one minute
# and 16 s the next. At 10 s, 7 of 28 fetches lost across four runs, and each
# loss ships the alert with no geometry that cycle, so the entity's polygon
# flickered poll to poll. 25 s is the useful ceiling: the coordinator's own
# budget is DEFAULT_TIMEOUT (30 s) and it wraps the index fetches too.
_GEOMETRY_FETCH_TIMEOUT = 25  # seconds

# The geometry phase as a whole ends this many seconds before the entry's poll
# budget does, whatever the per-file ceiling says. The per-file number only
# protects a poll whose events fit in one concurrency batch; past that, one
# slow file in the first batch leaves the second batch nothing, the poll's
# own timeout fires, and the whole entry fails the cycle, every alert
# unavailable for a poll interval. Fetches still running at the deadline
# ship their alert without geometry instead, the same degradation a single
# lost fetch gets. The margin covers building the alerts and the GPS pass.
_GEOMETRY_PHASE_MARGIN = 3  # seconds


# ---------------------------------------------------------------------------
# RSS index parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IndexItem:
    """One RSS index item — the complete record for one GDACS event."""

    event_type: str
    event_id: str
    episode_id: str
    alert_level: str
    episode_alert_level: str
    title: str
    description: str
    from_date: str
    to_date: str
    date_modified: str
    is_current: str
    country: str
    iso3: str
    severity_text: str
    population: str
    link: str


def _item_text(item: Any, name: str) -> str:
    """Read an RSS item child by local name, ignoring its namespace.

    Most of the fields this provider needs live in the ``gdacs:`` extension
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


def _parse_index(
    xml_text: str,
    *,
    event_types: Sequence[str] | None = None,
    min_level: str = "",
) -> list[_IndexItem]:
    """Parse one RSS index into items passing both filters.

    ``event_types`` of ``None`` (or empty) means no event-type narrowing at
    all, so a hazard code GDACS adds later still arrives; a non-empty
    selection keeps only those codes. ``min_level`` is the alert-level floor,
    empty meaning no floor. Unrankable levels fail open on both sides of that
    comparison: an item whose level is outside the known scale passes any
    floor (a label GDACS adds later must not vanish for exactly the users who
    raised the floor), and a floor outside the scale is no floor at all.

    Filtering here rather than after the fact is the volume guard: the
    current-events index runs to 315 items, 280 of them green wildfires, and
    each survivor costs a geometry fetch. Items missing either identity field
    are skipped — without both, neither the geometry URL nor the alert id can
    be built. Raises ``ET.ParseError`` on malformed XML; the caller converts
    that to ``UpdateFailed``.
    """
    wanted = {code.strip().upper() for code in event_types or () if code.strip()}
    floor = (_alert_level_rank(min_level) or 0) if min_level else 0
    root = ET.fromstring(xml_text)
    items: list[_IndexItem] = []
    for node in root.iter("item"):
        event_type = _item_text(node, "eventtype").upper()
        event_id = _item_text(node, "eventid")
        if not event_type or not event_id:
            continue
        if wanted and event_type not in wanted:
            continue
        alert_level = _item_text(node, "alertlevel")
        rank = _alert_level_rank(alert_level)
        if rank is not None and rank < floor:
            continue
        items.append(
            _IndexItem(
                event_type=event_type,
                event_id=event_id,
                episode_id=_item_text(node, "episodeid"),
                alert_level=alert_level,
                episode_alert_level=_item_text(node, "episodealertlevel"),
                title=_item_text(node, "title"),
                description=_item_text(node, "description"),
                from_date=_item_text(node, "fromdate"),
                to_date=_item_text(node, "todate"),
                date_modified=_item_text(node, "datemodified"),
                is_current=_item_text(node, "iscurrent"),
                country=_item_text(node, "country"),
                iso3=_item_text(node, "iso3"),
                severity_text=_item_text(node, "severity"),
                population=_item_text(node, "population"),
                link=_item_text(node, "link"),
            )
        )
    return items


def _merge_indexes(*batches: Sequence[_IndexItem]) -> list[_IndexItem]:
    """Union index items by event, keeping the most recently modified.

    The two indexes overlap without either containing the other, and a
    long-running event appears in both — under different episodes, since one
    may have been generated before the latest re-issue. Keying on
    ``(eventtype, eventid)`` collapses those to one alert, which is also what
    makes the union safe to widen later: a third index would add events, never
    duplicates.
    """
    latest: dict[tuple[str, str], _IndexItem] = {}
    for batch in batches:
        for item in batch:
            key = (item.event_type, item.event_id)
            existing = latest.get(key)
            if existing is None or _rfc822(item.date_modified) >= _rfc822(
                existing.date_modified
            ):
                latest[key] = item
    return list(latest.values())


def _rfc822(value: str) -> str:
    """Convert an RFC 822 feed timestamp to ISO 8601, or "" if unparseable.

    RSS dates arrive as ``"Sat, 08 Aug 2026 17:58:00 GMT"``; every timestamp
    consumer downstream — phase computation, the onset split, the store's
    retention clock — parses ISO 8601. Returning "" rather than the raw string
    keeps an unparseable date indistinguishable from an absent one, which is
    how those consumers already treat a field they cannot read.

    ISO strings sort chronologically as text once they share an offset, which
    is what lets ``_merge_indexes`` compare them directly.
    """
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return ""


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
    actually names the *event*: its type and its id.
    """
    return hashlib.sha256(f"{event_type}:{event_id}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _geojson_url(item: _IndexItem) -> str:
    """Per-episode geometry URL, or "" for an item with no episode id."""
    if not item.episode_id:
        return ""
    return GDACS_GEOJSON_URL.format(
        eventtype=item.event_type,
        eventid=item.event_id,
        episodeid=item.episode_id,
    )


def _feature_class(feature: object) -> str:
    """The ``Class`` property of a GeoJSON feature, or "" when it has none."""
    if not isinstance(feature, dict):
        return ""
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return ""
    return str(properties.get("Class") or "")


def _shapes_from_geojson(
    body: str,
) -> tuple[list[list[list[float]]], list[list[float]]]:
    """Split a GDACS event FeatureCollection into ``(polygon rings, points)``.

    Returns two empty lists for anything that is not a usable
    FeatureCollection, which is the load-bearing case rather than a defensive
    one: GDACS answers a missing file with HTTP 200 and an HTML page, so the
    content is the only thing that distinguishes a real payload from a miss.

    Forecast classes are excluded, and a flood's country outline is excluded
    when its real footprint is in the same file (see ``_AFFECTED_CLASS``).

    Coordinates arrive in GeoJSON ``[lon, lat]`` order already, so nothing is
    flipped here.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return [], []
    if not isinstance(parsed, dict):
        return [], []
    features = parsed.get("features")
    if not isinstance(features, list):
        return [], []

    classes = [_feature_class(feature) for feature in features]
    has_footprint = _AFFECTED_CLASS in classes

    rings: list[list[list[float]]] = []
    points: list[list[float]] = []
    for feature, klass in zip(features, classes):
        if not isinstance(feature, dict):
            continue
        if klass.startswith(_FORECAST_CLASS_PREFIXES):
            continue
        if has_footprint and klass == _COUNTRY_OUTLINE_CLASS:
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        if coordinates is None:
            continue
        gtype = geometry.get("type")
        try:
            if gtype == "Point":
                points.append([float(coordinates[0]), float(coordinates[1])])
            elif gtype == "Polygon":
                rings.append([[float(x), float(y)] for x, y in coordinates[0]])
            elif gtype == "MultiPolygon":
                rings.extend(
                    [[float(x), float(y)] for x, y in polygon[0]]
                    for polygon in coordinates
                    if polygon
                )
        except (TypeError, ValueError, IndexError):
            continue
    return rings, points


# ---------------------------------------------------------------------------
# CAPAlert construction
# ---------------------------------------------------------------------------


def _parameters(item: _IndexItem) -> dict[str, str]:
    """GDACS-native fields with no CAP home, as alert attributes.

    ``todate`` lands here rather than in ``expires`` or ``ends`` — it is the
    last observation time, in the past for every live event in the feed, so
    either of those would announce that a running hazard has finished.
    """
    values = {
        "eventtype": item.event_type,
        "eventid": item.event_id,
        "episodeid": item.episode_id,
        "alertlevel": item.alert_level,
        "episodealertlevel": item.episode_alert_level,
        "iscurrent": item.is_current,
        "todate": _rfc822(item.to_date),
        "gdacs_severity": item.severity_text,
        "population": item.population,
        "iso3": item.iso3,
    }
    return {key: value for key, value in values.items() if value}


def _build_alert(
    item: _IndexItem,
    rings: list[list[list[float]]],
    points: list[list[float]],
) -> CAPAlert:
    """Build a ``CAPAlert`` from one index item and its fetched geometry."""
    category, certainty = GDACS_CAP_CLASSIFICATION.get(item.event_type, ("", ""))
    onset = _rfc822(item.from_date)
    return CAPAlert(
        id=_compute_gdacs_id(item.event_type, item.event_id),
        url=item.link,
        # The identifier GDACS would have put on the CAP body, rebuilt from the
        # two fields it builds it from. Not identity — see _compute_gdacs_id —
        # but it moves with the episode, so a re-issue is visible.
        identifier=f"GDACS_{item.event_type}_{item.event_id}_{item.episode_id}",
        # The hazard label, not the feed's own <event> text: GDACS writes
        # "Volcano Eruption" on a volcano body, which matches no icon keyword,
        # while the code maps to the exact name the icon table is keyed on.
        event=GDACS_EVENT_TYPES.get(item.event_type, item.event_type),
        msg_type=_MSG_TYPE,
        status=_STATUS,
        scope=_SCOPE,
        category=category,
        urgency=_URGENCY,
        severity=_ALERT_LEVEL_SEVERITY.get(item.alert_level.strip().capitalize(), ""),
        certainty=certainty,
        sent=_rfc822(item.date_modified),
        effective=onset,
        onset=onset,
        headline=item.title,
        description=item.description,
        web=item.link,
        # The country the event is in, which is what GDACS names its area by.
        # The CAP bodies wrote the literal string "Polygon" in this slot.
        area_desc=item.country,
        geometry=geometry_from_shapes(rings, points),
        points=tuple((lon, lat) for lon, lat in points),
        sender=_SENDER,
        sender_name=_SENDER_NAME,
        parameters=_parameters(item) or None,
        provider="gdacs",
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


# ``(polygon rings, points)`` as _shapes_from_geojson returns them.
_Shapes = tuple[list[list[list[float]]], list[list[float]]]


class GDACSProvider:
    """GDACS RSS indexes → CAPAlert, with per-episode GeoJSON geometry.

    One instance lives as long as its config entry, and it remembers the last
    usable shapes it built for each event (#203). A geometry fetch that fails,
    runs past the poll deadline, or answers with GDACS's HTML "no such file"
    page would otherwise ship the alert shape-less for the cycle and the
    coordinator would purge the stored polygon with it, so the entity's shape
    flickered with gdacs.org's mood. A stale footprint beats none: episodes
    advance over hours to days, so the remembered shape is this episode's or
    the previous one's. The memory is pruned to the events the indexes still
    list, since withdrawal is the only end-of-life signal GDACS has.
    """

    def __init__(self) -> None:
        self._last_shapes: dict[tuple[str, str], _Shapes] = {}

    @property
    def name(self) -> str:
        return "gdacs"

    async def async_validate_config(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        *,
        user_agent: str | None = None,
    ) -> str | None:
        """Nothing to validate: the scope is the whole planet.

        Both indexes are fixed and global, and the optional GPS filter narrows
        the result rather than selecting a feed — a point with no events near
        it is a quiet week, not a misconfiguration.
        """
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
        """Fetch active alerts from the GDACS indexes.

        (a) Fetches both RSS indexes concurrently.
        (b) Filters each by event type and alert level, then unions them.
        (c) Fetches per-episode GeoJSON for each survivor (bounded concurrency,
            shared cache).
        (d) Builds CAPAlert objects and applies the optional GPS filter.
        """
        # Clock the poll budget from here, before the index fetches, since the
        # coordinator's timeout wraps those too.
        poll_budget = float(options.get(CONF_TIMEOUT) or DEFAULT_TIMEOUT)
        deadline = asyncio.get_running_loop().time() + max(
            poll_budget - _GEOMETRY_PHASE_MARGIN, 1.0
        )
        headers = {"User-Agent": user_agent} if user_agent else None
        bodies = await asyncio.gather(
            self._fetch_index(session, GDACS_RSS_CURRENT_URL, headers),
            self._fetch_index(session, GDACS_RSS_24H_URL, headers),
            return_exceptions=True,
        )

        event_types = options.get(CONF_GDACS_EVENT_TYPES)
        # Unset means the Orange floor, not "no floor" — see the constant. This
        # is the only GDACS option whose default narrows rather than widens.
        min_level = str(options.get(CONF_ALERT_LEVEL) or GDACS_DEFAULT_ALERT_LEVEL)
        batches: list[list[_IndexItem]] = []
        failures: list[str] = []
        for url, body in zip((GDACS_RSS_CURRENT_URL, GDACS_RSS_24H_URL), bodies):
            if isinstance(body, BaseException):
                failures.append(f"{url}: {body}")
                continue
            try:
                batches.append(
                    _parse_index(body, event_types=event_types, min_level=min_level)
                )
            except ET.ParseError as err:
                failures.append(f"{url}: malformed XML: {err}")

        # One index failing is survivable and the union degrades to the other:
        # losing the current-events feed costs the long-lived events, losing
        # the 24-hour feed costs the sub-threshold ones. Losing both leaves
        # nothing to distinguish an outage from a world with no disasters in
        # it, and the coordinator must not read that as every alert ending.
        if not batches:
            raise UpdateFailed(f"GDACS: no index available ({'; '.join(failures)})")
        for failure in failures:
            _LOGGER.warning(
                "GDACS: index unavailable, continuing without it: %s", failure
            )

        items = _merge_indexes(*batches)
        if not items:
            return []

        cache = (
            cap_content_cache if cap_content_cache is not None else CAPContentCache()
        )
        # Ten, not the five the CAP-body fetch used: doubling the concurrency
        # halved the wall time on a 40-event sample (6.0 s → 3.2 s, measured
        # 2026-08-08, when every hazard but cyclones weighed 2–10 KiB). Floods
        # and droughts have since grown to hundreds of KiB and beyond, which is
        # what _GEOMETRY_FETCH_TIMEOUT answers; the fan-out is still right.
        semaphore = asyncio.Semaphore(10)
        unfinished: list[str] = []
        reused: list[str] = []

        async def _fetch_shapes(url: str) -> _Shapes:
            """This poll's shapes for ``url``, empty on any kind of miss."""
            try:
                # One deadline for the phase, applied per task so a fetch that
                # finished keeps its result when a sibling does not.
                async with asyncio.timeout_at(deadline):
                    async with semaphore:
                        body = await cache.get_or_fetch(
                            session,
                            url,
                            user_agent=user_agent,
                            timeout=_GEOMETRY_FETCH_TIMEOUT,
                        )
            except TimeoutError:
                unfinished.append(url)
                return [], []
            if body is None:
                _LOGGER.warning("GDACS: geometry fetch failed for %s", url)
                return [], []
            shapes = _shapes_from_geojson(body)
            if not shapes[0] and not shapes[1]:
                # HTTP 200 with an HTML body is how GDACS says "no such file",
                # so this is a normal miss for a hazard type that publishes no
                # geometry rather than a fault. The alert still ships, geocode-
                # and text-only; only the GPS filter can no longer place it.
                _LOGGER.debug("GDACS: no usable geometry in %s", url)
            return shapes

        async def _geometry_for(item: _IndexItem) -> _Shapes:
            url = _geojson_url(item)
            if not url:
                return [], []
            key = (item.event_type, item.event_id)
            shapes = await _fetch_shapes(url)
            if shapes[0] or shapes[1]:
                self._last_shapes[key] = shapes
                return shapes
            previous = self._last_shapes.get(key)
            if previous is None:
                return shapes
            reused.append(url)
            return previous

        shapes = await asyncio.gather(*[_geometry_for(item) for item in items])
        # Withdrawal from the indexes is the end of an event; so is falling
        # outside the entry's type or level filter, which is the same absence
        # from where this method stands.
        listed = {(item.event_type, item.event_id) for item in items}
        for key in [k for k in self._last_shapes if k not in listed]:
            del self._last_shapes[key]
        if reused:
            _LOGGER.warning(
                "GDACS: %d of %d alerts carry a previous poll's geometry; this "
                "cycle's fetch failed, was unfinished, or had no usable shape",
                len(reused),
                len(items),
            )
        if unfinished:
            _LOGGER.warning(
                "GDACS: geometry phase reached the poll budget (%ss); %d of %d "
                "fetches unfinished, those alerts ship without geometry this "
                "cycle. A higher timeout or a narrower alert level helps",
                poll_budget,
                len(unfinished),
                len(items),
            )
        alerts = [
            _build_alert(item, rings, points)
            for item, (rings, points) in zip(items, shapes)
        ]

        gps_loc = config.get(CONF_GPS_LOC)
        if gps_loc:
            return self._filter_by_polygon(alerts, gps_loc)
        return alerts

    @staticmethod
    async def _fetch_index(
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str] | None,
    ) -> str:
        """Fetch one RSS index, raising ``UpdateFailed`` on a non-200."""
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"HTTP {resp.status}")
            return await resp.text()

    @staticmethod
    def _filter_by_polygon(alerts: list[CAPAlert], gps_loc: str) -> list[CAPAlert]:
        """Keep alerts whose geometry contains the configured GPS point.

        Fails loud when the feed has alerts but none carry polygons, matching
        the WMO/ECCC/MeteoAlarm GPS-mode contract. GDACS encodes even a point
        hazard as an area — an earthquake ships a 100 km circle around the
        epicentre — so in practice this mode answers "did it happen near me",
        and the guard catches the geometry host going down rather than a feed
        that genuinely publishes none.
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
