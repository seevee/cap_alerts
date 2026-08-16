"""CAPAlert dataclass — provider-agnostic alert model based on CAP 1.2."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any

# Immutable empty mapping shared as the ``geocodes`` default. A plain ``{}``
# would be rejected as a mutable default on a frozen/slots dataclass, so the
# field uses ``default_factory`` returning this singleton.
_EMPTY_GEOCODES: Mapping[str, tuple[str, ...]] = MappingProxyType({})

# Promoted geocode schemes: accessor alias → ordered accept-list of CAP
# ``valueName``s, first non-empty wins.
#
# ``geocodes`` is the complete area-geocode surface — every scheme a feed
# publishes lands there under its raw ``valueName``, and a new scheme needs no
# model change. An alias is a stable short name for a scheme integration code
# reaches for often enough to deserve one:
#   geocode_ugc / geocode_same  — zone matching
#   geocode_clc                 — ECCC marine detection (province-numbered for
#                                 land zones, "00…" for marine/water zones)
#   geocode_sgc                 — StatCan SGC codes, what ECCC province
#                                 filtering matches on (makes it debuggable)
# The accept-list absorbs a source bumping its scheme version (e.g.
# ``…:1.0:CLC`` → ``…:1.1:CLC``) without a provider change.
#
# Aliases are read paths only — ``to_attributes()`` publishes the container and
# not the views, so no consumer sees the same codes twice (issue #150).
GEOCODE_SCHEME_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "geocode_ugc": ("UGC",),
        "geocode_same": ("SAME",),
        "geocode_clc": ("layer:EC-MSC-SMC:1.0:CLC",),
        "geocode_sgc": ("profile:CAP-CP:Location:0.3",),
    }
)


def geocodes_from(
    raw: Mapping[str, Iterable[str]],
) -> Mapping[str, tuple[str, ...]]:
    """Normalize a provider's scheme→codes mapping into a ``geocodes`` container.

    The single funnel every provider routes its area geocodes through: values
    are de-duplicated order-preserving, empty schemes and empty values are
    dropped, and the result is immutable. Returns the shared empty singleton
    when nothing survives.
    """
    normalized: dict[str, tuple[str, ...]] = {}
    for scheme, values in raw.items():
        if not scheme:
            continue
        codes: list[str] = []
        for value in values:
            if value and value not in codes:
                codes.append(value)
        if codes:
            normalized[scheme] = tuple(codes)
    if not normalized:
        return _EMPTY_GEOCODES
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class CAPAlert:
    """Provider-agnostic alert modeled on CAP 1.2. All fields optional except id."""

    # -- Identity --
    id: str
    url: str = ""
    identifier: str = ""

    # -- Classification (CAP required) --
    event: str = ""
    msg_type: str = ""
    status: str = ""
    scope: str = ""
    category: str = ""
    urgency: str = ""
    severity: str = ""
    certainty: str = ""
    response_type: str = ""

    # -- Timestamps (ISO 8601 strings as received) --
    sent: str = ""
    effective: str = ""
    onset: str = ""
    expires: str = ""
    ends: str | None = None

    # -- Content --
    headline: str = ""
    description: str = ""
    instruction: str | None = None
    note: str = ""
    web: str = ""

    # -- Geography --
    area_desc: str = ""
    affected_zones: tuple[str, ...] = ()
    affected_zone_uris: tuple[str, ...] = ()
    # Every area geocode a feed publishes, keyed by raw CAP ``valueName`` (e.g.
    # ``UGC``, ``SAME``, ``EMMA_ID``, ``NUTS3``, ``layer:EC-MSC-SMC:1.0:CLC``).
    # The complete geocode surface for all providers, and the only one published
    # as an attribute — raw keys cannot mislabel a scheme, and well-known
    # schemes are reachable in code through the ``geocode_*`` accessors below
    # (see ``GEOCODE_SCHEME_ALIASES``). Build it with ``geocodes_from()``.
    # Serialized as ``{scheme: [codes]}``, omitted if empty.
    geocodes: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: _EMPTY_GEOCODES
    )
    geometry: dict | None = None
    geometry_ref: str = ""
    bbox: tuple[float, float, float, float] | None = None
    # Point locations the feed published, as ``(lon, lat)`` pairs derived from
    # zero-radius CAP ``<circle>`` elements (issue #27). A list because
    # ``<circle>`` is 0..* per ``<area>`` (CAP 1.2 §3.2.4).
    #
    # Deliberately *not* named for what a point means: CAP defines circles as
    # part of the affected area, unioned with any polygons, so reading one as
    # "where the incident is" is a sender's convention (NSW RFS publishes a
    # street-address incident marker this way) and not something the standard
    # blesses. This field records the geometry; consumers decide the meaning.
    #
    # Published alongside ``geometry`` rather than replacing it, so an alert
    # carrying both a fire-ground polygon and a location point keeps each.
    points: tuple[tuple[float, float], ...] = ()
    # Marine/water-zone classification, set per-provider (NWS UGC marine-area
    # prefix, ECCC CLC "00…"). Drives the opt-in exclude-marine filter and is
    # surfaced as an attribute only when True.
    is_marine: bool = False

    # -- Event Codes --
    event_code_nws: str = ""
    event_code_same: str = ""

    # -- VTEC (NWS-specific) --
    vtec: tuple[str, ...] = ()
    vtec_office: str = ""
    vtec_phenomena: str = ""
    vtec_significance: str = ""
    vtec_action: str = ""
    vtec_tracking: str = ""

    # -- Sender --
    sender: str = ""
    sender_name: str = ""

    # -- References / Lifecycle --
    references: tuple[str, ...] = ()
    replaced_by: str = ""
    replaced_at: str = ""
    # Reserved for sub-incident relationships (RFC §6.3). Never populated in
    # v1; ``to_attributes()`` skips empty strings so the attribute stays
    # absent until a future provider sets it.
    parent_id: str = ""

    # -- NWS Parameters (catch-all) --
    parameters: dict | None = None

    # -- Alternate language content (populated when available) --
    # ``event_alt`` exists for classification, not display: ``<event>`` is CAP
    # free text, so a localized one matches no icon keyword. Keeping the
    # alternate block's event lets ``icons.icon_for`` classify on English while
    # the user still reads their own language (issue #91).
    event_alt: str = ""
    headline_alt: str = ""
    description_alt: str = ""
    instruction_alt: str | None = None
    language: str = ""  # BCP-47 of primary content (e.g. "en-CA")
    language_alt: str = ""  # BCP-47 of alternate content (e.g. "fr-CA")

    # -- Episode (MeteoFrance multi-day merge) --
    # Per-day profile of a merged episode, ordered by date, one entry per
    # forecast day. Populated only by the MeteoAlarm provider for MeteoFrance,
    # which publishes one warning per calendar day rather than one per episode
    # (issue #37); empty for every other sender and provider. The merged alert
    # carries the dominant day's content, so this is where per-day truth lives.
    # Keys: date, onset, expires, severity, awareness_level, event, headline,
    # area_desc. Serialized by ``to_attributes``'s tuple branch as a list of
    # objects, and omitted while empty.
    episode_days: tuple[dict[str, str], ...] = ()

    # -- Provider --
    provider: str = "nws"

    # -- Normalization metadata (set by integration, not providers) --
    severity_normalized: str = ""
    phase: str = ""
    # The one exception in this group: a provider-supplied *input* to
    # ``normalize._compute_phase``, not an output of it. Carries provider-native
    # lifecycle vocabulary — ECCC's ``ended`` / ``transitioned_out``, read from
    # the ``Alert_Location_Status`` CAP parameter of the selected ``<info>``
    # block — for feeds that signal termination somewhere other than
    # ``msgType``. Empty for providers that publish no such signal, which keeps
    # phase computation bit-identical for them.
    lifecycle_status: str = ""
    icon: str = ""

    # -- State transition metadata (set by alert store) --
    previous_phase: str = ""
    phase_changed: bool = False
    # Retention markers (RFC §2.1). ``stale`` says the most recent
    # reconciliation did not observe this alert but the store kept it anyway
    # rather than treating one absence as a lifecycle signal (RFC §1.4 item 8);
    # ``last_confirmed`` is the ISO timestamp of the last reconciliation that
    # did see it. Both are meaningless while an alert is being observed
    # normally, so ``stale`` is omitted from attributes when False and
    # ``last_confirmed`` is only stamped once an alert has gone unconfirmed.
    stale: bool = False
    last_confirmed: str = ""

    # -- Promoted geocode schemes (derived from ``geocodes``) --
    # Read-only aliases, not fields: ``geocodes`` is the single source of truth,
    # so a provider cannot populate an alias and the container inconsistently.
    # They are also not attributes — see ``to_attributes()``.

    def _promoted_geocode(self, alias: str) -> tuple[str, ...]:
        """Codes for the first accepted ``valueName`` of a promoted scheme."""
        for scheme in GEOCODE_SCHEME_ALIASES[alias]:
            codes = self.geocodes.get(scheme)
            if codes:
                return tuple(codes)
        return ()

    @property
    def geocode_ugc(self) -> tuple[str, ...]:
        """NWS Universal Geographic Code zones (``UGC``)."""
        return self._promoted_geocode("geocode_ugc")

    @property
    def geocode_same(self) -> tuple[str, ...]:
        """FIPS-based SAME/FIPS6 area codes (``SAME``)."""
        return self._promoted_geocode("geocode_same")

    @property
    def geocode_clc(self) -> tuple[str, ...]:
        """ECCC Canadian Location Codes (``layer:EC-MSC-SMC:1.0:CLC``)."""
        return self._promoted_geocode("geocode_clc")

    @property
    def geocode_sgc(self) -> tuple[str, ...]:
        """StatCan SGC location codes (``profile:CAP-CP:Location:0.3``)."""
        return self._promoted_geocode("geocode_sgc")

    def to_attributes(self) -> dict[str, Any]:
        """Flat attribute dict. Omits empty/None/False values (except id).

        Full ``geometry`` is never included — consumers fetch polygons out-of-band
        via the ``geometry_ref`` handle (see websocket command ``cap_alerts/geometry``
        and REST endpoint ``/api/cap_alerts/geometry/{geometry_ref}``).

        The promoted ``geocode_*`` aliases are **not** serialized: they are the
        same codes ``geocodes`` already carries under their raw ``valueName``,
        and publishing both put the geocode surface twice on the wire — 5,510
        bytes of one live ECCC alert's 19,080-byte payload, on the alert that
        overflowed the recorder's ceiling (issue #150). The aliases remain as
        typed accessors for integration code; a consumer reads the container.
        """
        attrs: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "geometry":
                continue
            val = getattr(self, f.name)
            if f.name in ("is_marine", "stale") and not val:
                continue
            if f.name == "geocodes":
                if val:
                    attrs[f.name] = {
                        scheme: list(codes) for scheme, codes in val.items()
                    }
                continue
            if val is None or val == "" or val == ():
                continue
            if isinstance(val, tuple):
                attrs[f.name] = list(val)
            else:
                attrs[f.name] = val
        return attrs
