"""CAPAlert dataclass — provider-agnostic alert model based on CAP 1.2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any

# Immutable empty mapping shared as the ``geocodes`` default. A plain ``{}``
# would be rejected as a mutable default on a frozen/slots dataclass, so the
# field uses ``default_factory`` returning this singleton.
_EMPTY_GEOCODES: Mapping[str, tuple[str, ...]] = MappingProxyType({})


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
    geocode_ugc: tuple[str, ...] = ()
    geocode_same: tuple[str, ...] = ()
    # ECCC Canadian Location Code (layer:EC-MSC-SMC:1.0:CLC). Province-numbered
    # for land zones; "00…" for marine/water zones (drives marine detection).
    geocode_clc: tuple[str, ...] = ()
    # Per-scheme area geocodes keyed by CAP ``valueName`` (e.g. ``EMMA_ID``,
    # ``NUTS3``, ``WARNCELLID``). Typed multi-scheme container for providers
    # whose feeds carry a mix of schemes (MeteoAlarm); populated instead of the
    # per-scheme named fields above where a single named field can't honestly
    # hold "the" geocode. Serialized as ``{scheme: [codes]}``, omitted if empty.
    geocodes: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: _EMPTY_GEOCODES
    )
    geometry: dict | None = None
    geometry_ref: str = ""
    bbox: tuple[float, float, float, float] | None = None
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
    headline_alt: str = ""
    description_alt: str = ""
    instruction_alt: str | None = None
    language: str = ""  # BCP-47 of primary content (e.g. "en-CA")
    language_alt: str = ""  # BCP-47 of alternate content (e.g. "fr-CA")

    # -- Provider --
    provider: str = "nws"

    # -- Normalization metadata (set by integration, not providers) --
    severity_normalized: str = ""
    phase: str = ""
    icon: str = ""

    # -- State transition metadata (set by alert store) --
    previous_phase: str = ""
    phase_changed: bool = False

    def to_attributes(self) -> dict[str, Any]:
        """Flat attribute dict. Omits empty/None/False values (except id).

        Full ``geometry`` is never included — consumers fetch polygons out-of-band
        via the ``geometry_ref`` handle (see websocket command ``cap_alerts/geometry``
        and REST endpoint ``/api/cap_alerts/geometry/{geometry_ref}``).
        """
        attrs: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "geometry":
                continue
            val = getattr(self, f.name)
            if f.name == "is_marine" and not val:
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
