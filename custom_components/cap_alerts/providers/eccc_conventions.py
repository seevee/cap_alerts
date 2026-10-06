"""ECCC conventions: the marine CLC block, lifecycle tokens, the successor hook.

One source's interpretive rules, exposed to the provider as ``CONVENTIONS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..const import REMOVAL_REASON_ENDED, REMOVAL_REASON_SUPERSEDED
from ..conventions import SourceConventions
from ..model import CAPAlert

# ECCC Canadian Location Codes are province-numbered for land zones; marine and
# water zones are the "00…" block.
ECCC_MARINE_CLC_PREFIX = "00"

# ECCC ``Alert_Location_Status`` tokens that mean the alert has reached
# end-of-life for the area it was selected for, whatever its ``msgType`` and
# ``expires`` still say, each mapped to what it means for a consumer. Unknown
# values are deliberately absent so an unfamiliar token degrades to msg_type
# handling rather than silently retiring a live alert.
#
# The keys are also the terminal set — "this token ends the alert" and "here is
# why" are one fact, so they are one declaration (issue #108). ``ended`` is an
# all-clear for this area group; ``cancelled`` is the forecaster stopping it
# early, the same all-clear to a consumer (observed live 2026-08-23 on both
# ``Alert_Location_Status`` and the CAM threat-area DLC geocode, issue #172);
# ``transitioned_out`` means the area moved to a different alert, whose own
# ``incident_created`` carries the same news.
ECCC_LIFECYCLE_REMOVAL_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "ended": REMOVAL_REASON_ENDED,
        "cancelled": REMOVAL_REASON_ENDED,
        "transitioned_out": REMOVAL_REASON_SUPERSEDED,
    }
)

# CAP parameter naming the successor of a ``transitioned_out`` area group
# (issue #190). Only the 1.1 layer has been observed — 27/27 measured
# transitioned_out groups on a 4-day ``alertsarchive.pelmorex.com`` sample
# carried it, none carried a 1.0 predecessor — unlike ``Alert_Name`` and
# ``Alert_Location_Status``, which do.
_TRANSITIONED_OUT_REFERENCE_PARAM_KEY = (
    "layer:EC-MSC-SMC:1.1:Transitioned_Out_CAP_Reference"
)


def eccc_superseded_by(alert: CAPAlert) -> str | None:
    """The successor CAP identifier for an ECCC ``transitioned_out`` ending.

    The parameter's value is CAP ``<references>`` syntax
    (``sender,identifier,sent``) with an ECCC-appended ``;<CLC>`` suffix — the
    CLC is one of the block's own area codes, not part of the reference. Only
    the identifier is surfaced; ``sender``/``sent`` are validated shape, not
    published. The identifier field is joined back from every middle segment
    (mirroring ``providers/cap.py::_parse_references``), tolerant of a
    comma-bearing identifier.

    Returns ``None`` when the parameter is absent or does not parse as that
    shape. Measured to dangle — no matching document ever reaches NAAD — in 18
    of 27 cases; that is a known feed gap, not a malformed message, so callers
    must treat ``None`` as "nothing to publish", not an error.
    """
    if not alert.parameters:
        return None
    raw = alert.parameters.get(_TRANSITIONED_OUT_REFERENCE_PARAM_KEY, "").strip()
    if not raw:
        return None
    reference, _, _clc = raw.partition(";")
    parts = reference.split(",")
    if len(parts) < 3:
        return None
    identifier = ",".join(parts[1:-1]).strip()
    return identifier or None


def resolve_language(language: str) -> str:
    """``auto`` on a bilingual EN/FR feed: one of the two full tags."""
    return "fr-CA" if language.startswith("fr") else "en-CA"


ECCC_CONVENTIONS = SourceConventions(
    marine_code_prefixes=frozenset({ECCC_MARINE_CLC_PREFIX}),
    lifecycle_removal_reasons=ECCC_LIFECYCLE_REMOVAL_REASONS,
    superseded_by=eccc_superseded_by,
    resolve_language=resolve_language,
)


# What this provider declares, keyed as ``conventions_for`` resolves it.
CONVENTIONS: Mapping[str, SourceConventions] = MappingProxyType(
    {
        "eccc": ECCC_CONVENTIONS,
    }
)
