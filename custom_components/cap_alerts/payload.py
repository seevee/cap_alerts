"""Bound the attribute payload the recorder stores (issue #150).

The recorder refuses to store a state's attributes once the serialized set
exceeds 16,384 bytes — it writes ``{}`` and logs, so the row keeps the state and
loses every attribute on it. One live ECCC air-quality warning serializes to
19,080 bytes today, which is that failure happening in production rather than
waiting to.

**The unit is the payload, not the field.** Per-field caps were measured against
425 real alerts (``scripts/text_size_sweep.py``) and fail in both directions at
once: an NWS Tropical Cyclone Local Statement carries 8,871 bytes of long-form
text and still serializes to 14,290, so a 6 KB ``description`` cap would shred
2,639 bytes of a hurricane statement that was never a problem — while the alert
that *does* overflow is only 9,535 bytes of text out of 19,080, so capping text
never rescues it at all.

So: serialize, measure, and trim only when it doesn't fit. Priority decides who
pays, strictly — the whole of one field's expendable text is spent before the
next field gives up a byte. Proportional shaving would damage the field the user
needs in order to spare the one nobody reads.

Measurement mirrors ``recorder.db_schema.shared_attrs_bytes_from_event``: the
ceiling applies to ``state.attributes`` minus ``ALL_DOMAIN_EXCLUDE_ATTRS`` and
minus the entity's own ``_unrecorded_attributes``, which is a smaller set than
``to_attributes()`` returns. Declaring ``parameters`` unrecorded therefore takes
the one unbounded provider-controlled term out of the bound for free.

Issue #245 found the other one. A Saskatchewan frost advisory covering 291 areas
carried 14,072 bytes of ``area_desc`` and 12,245 of ``geocodes`` (291 CLC plus
847 SGC codes) against 1,837 bytes of text: the ladder spent every text field
and still left the payload 10 KB over. ``geocodes`` is unrecorded now, like
``parameters``, so it stays complete on the live state (the card and automations
read it there) and only history loses it. ``area_desc`` joined the ladder ahead
of the primary text, so a list of 291 municipality names is what gets cut before
the description does.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ``recorder.db_schema.MAX_STATE_ATTRS_BYTES``. Not imported: the recorder is a
# separate integration and this one does not depend on it.
RECORDER_CEILING = 16384

# Headroom for what Home Assistant appends after ``extra_state_attributes``
# returns — ``friendly_name`` (device name plus the event, which normalization
# has already clipped to 255 characters), ``icon`` and ``device_class``.
PAYLOAD_RESERVE = 584

PAYLOAD_BUDGET = RECORDER_CEILING - PAYLOAD_RESERVE

# ``recorder.const.ALL_DOMAIN_EXCLUDE_ATTRS``, spelled out for the same reason
# as the ceiling above: ``homeassistant.const.ATTR_ATTRIBUTION``,
# ``ATTR_RESTORED`` and ``ATTR_SUPPORTED_FEATURES``.
_RECORDER_EXCLUDED = frozenset({"attribution", "restored", "supported_features"})

# Attributes the alert entity declares unrecorded, so they neither count toward
# the bound nor land in history. Both are unbounded and source-controlled.
# ``parameters`` is the providers' verbatim ``<parameter>`` catch-all, already
# excluded from ``store.CHANGED_FIELDS_ALLOWLIST``, so nothing downstream diffs
# it. ``geocodes`` grows with the area count — 1,138 codes on the #245 advisory —
# and every known consumer (the card's zone filter, templates, automations)
# reads it off the live state, which the recorder's exclusion never touches.
# Exporters fed by ``state_changed`` (InfluxDB, MQTT statestream) still see it
# in full; only the recorder's own history goes without.
UNRECORDED_ATTRIBUTES = frozenset({"parameters", "geocodes"})

# Long-form text, in the order it is spent. Both alternates go before either
# primary — the primary is the language the user asked for. The area list sits
# between them: a comma-joined run of place names reads as well cut short as
# whole, and on a wide alert it dwarfs the text (14,072 bytes for 291 areas on
# the #245 advisory, against 944 of description), so it pays before the primary
# text does. Within a language the instruction outlives the description: it is
# the protective-action text, and at a median 1,835 bytes across 6,158 sampled
# values protecting it outright is nearly free.
TRIM_PRIORITY: tuple[str, ...] = (
    "description_alt",
    "instruction_alt",
    "area_desc",
    "description",
    "instruction",
)

# The ladder is text only. It once ended on structural rungs — ``geocode_*``
# aliases republishing codes ``geocodes`` already carried, then NWS zone URIs
# that were a fixed prefix on codes ``geocodes`` also carried — and each came
# off at the source instead (issues #150, #292): paying the duplication back
# only on oversized alerts would have left every other alert carrying it.

# Below this many bytes a survivor is a stub rather than text, so the key is
# dropped instead. An attribute holding two words and an ellipsis tells a
# consumer less than its absence does.
_MIN_TEXT_KEEP = 160

# What every provider joins ``area_desc`` with (``providers/cap.py``,
# ``providers/meteoalarm.py``), and so where a cut list is backed off to.
_AREA_SEPARATOR = ", "

# Alert ids already reported as over budget after trimming, so the warning fires
# once per alert instead of on every state write (#245 was found in the
# recorder's log because this path only spoke at debug). Bounded by the number
# of distinct alerts that overflow in one process: a handful, if any.
_reported_over_budget: set[str] = set()


def truncate_bytes(text: str, limit_bytes: int) -> str:
    """Trim ``text`` to ``limit_bytes`` UTF-8 bytes, appending ``…``.

    Truncates at a UTF-8 character boundary to avoid mojibake. Under-limit
    input is returned unchanged.
    """
    if not text:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    # Reserve 3 bytes for the trailing ellipsis (U+2026 is 3 bytes in UTF-8).
    # A limit with no room for it yields the ellipsis alone rather than a
    # negative slice that would keep the text and drop its tail.
    if limit_bytes <= 3:
        return "\u2026"
    truncated = encoded[: limit_bytes - 3]
    # Back off to a character boundary by decoding with 'ignore'.
    return truncated.decode("utf-8", errors="ignore") + "\u2026"


def _cut_at_name_boundary(text: str) -> str:
    """Back a truncated area list off to its last whole name.

    ``truncate_bytes`` cuts on a character boundary, which for a comma-joined
    list means mid-name. Ending on a name the list actually contains reads
    right on a card and hands a consumer that splits the string no fragment.
    A list with no separator before the cut is one long name, left as cut.
    """
    body = text.removesuffix("\u2026")
    boundary = body.rfind(_AREA_SEPARATOR)
    if boundary <= 0:
        return text
    return body[:boundary] + "\u2026"


def measure(
    attrs: dict[str, Any],
    unrecorded: frozenset[str] = UNRECORDED_ATTRIBUTES,
) -> int | None:
    """Serialized size of the subset the recorder measures, or None if unmeasurable.

    ``None`` means the attributes did not survive JSON encoding, which is the
    recorder's problem to report — there is nothing useful to trim toward, so
    callers leave the payload alone rather than shredding text on a guess.
    """
    excluded = _RECORDER_EXCLUDED | unrecorded
    try:
        return len(_serialize({k: v for k, v in attrs.items() if k not in excluded}))
    except TypeError:
        return None


def _serialize(attrs: dict[str, Any]) -> bytes:
    """Encode the way the recorder's ``json_bytes`` does: compact, UTF-8 verbatim.

    Home Assistant serializes with orjson. The standard library produces the
    same bytes for the strings, numbers, lists and dicts an attribute set is
    made of, given no separators and no ASCII escaping; the one divergence is
    the spelling of an exponent-form float, one byte at most, well inside the
    reserve above. Not importing the helper is the point: this module measures
    a payload and owes nothing to the host that stores it.
    """
    return json.dumps(attrs, separators=(",", ":"), ensure_ascii=False).encode()


def fit_to_budget(
    attrs: dict[str, Any],
    *,
    budget: int = PAYLOAD_BUDGET,
    unrecorded: frozenset[str] = UNRECORDED_ATTRIBUTES,
) -> dict[str, Any]:
    """Return ``attrs`` trimmed to fit ``budget``, or unchanged if it already does.

    Never mutates the input: an over-budget payload is copied first, so the
    ``CAPAlert`` behind it keeps the full text the source sent and
    ``store.process()`` goes on diffing that rather than a platform artifact.
    """
    size = measure(attrs, unrecorded)
    if size is None or size <= budget:
        return attrs

    trimmed = dict(attrs)
    for key in TRIM_PRIORITY:
        text = trimmed.get(key)
        if not isinstance(text, str) or not text:
            continue
        # Everything this field can give up, minus what is actually needed:
        # removing a character never saves less than a byte (JSON escapes save
        # more), so the cut always clears the excess.
        room = len(text.encode("utf-8")) - (size - budget)
        if room < _MIN_TEXT_KEEP:
            del trimmed[key]
        elif key == "area_desc":
            trimmed[key] = _cut_at_name_boundary(truncate_bytes(text, room))
        else:
            trimmed[key] = truncate_bytes(text, room)
        size = measure(trimmed, unrecorded)
        if size is None or size <= budget:
            return trimmed

    alert_id = str(trimmed.get("id", "?"))
    log = _LOGGER.debug if alert_id in _reported_over_budget else _LOGGER.warning
    _reported_over_budget.add(alert_id)
    log(
        "Alert %s still exceeds the %d-byte attribute budget at %s bytes after "
        "trimming; the recorder will not store its attributes",
        alert_id,
        budget,
        size,
    )
    return trimmed
