"""CAPAlert dataclass — provider-agnostic alert model based on CAP 1.2."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


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
    geometry: dict | None = None
    bbox: tuple[float, float, float, float] | None = None

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

    # -- NWS Parameters (catch-all) --
    parameters: dict | None = None

    # -- Alternate language content (populated when available) --
    headline_alt: str = ""
    description_alt: str = ""
    instruction_alt: str | None = None
    language: str = ""       # BCP-47 of primary content (e.g. "en-CA")
    language_alt: str = ""   # BCP-47 of alternate content (e.g. "fr-CA")

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
        """Flat attribute dict. Omits empty/None/False values (except id)."""
        attrs: dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if val is None or val == "" or val == ():
                continue
            if isinstance(val, tuple):
                attrs[f.name] = list(val)
            else:
                attrs[f.name] = val
        return attrs
