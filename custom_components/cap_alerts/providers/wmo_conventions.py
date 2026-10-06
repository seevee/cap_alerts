"""WMO SWIC conventions: pure CAP, a verbatim language tag.

One source's interpretive rules, registered into ``conventions`` at import.
"""

from __future__ import annotations

from ..conventions import SourceConventions, register
from ..icons import INTERNATIONAL_EVENT_SUBSTRINGS, match_event_substrings
from ..model import CAPAlert


def wmo_icon(alert: CAPAlert, event: str) -> str | None:
    """The international vocabulary first; ~140 national services speak it."""
    return match_event_substrings(event, INTERNATIONAL_EVENT_SUBSTRINGS)


def resolve_language(language: str) -> str:
    """``auto`` passes the full tag through.

    SWIC bodies carry full tags (en-GB vs en-US, pt-PT vs pt-BR, zh-CN vs
    zh-HK), so truncating first would discard the distinction and pick
    arbitrarily. The shared matcher casefolds and degrades to the primary
    subtag on its own.
    """
    return language.strip() or "en"


WMO_CONVENTIONS = register(
    "wmo", SourceConventions(resolve_language=resolve_language, icon=wmo_icon)
)
