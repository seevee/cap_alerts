"""Geometry store — in-memory, LRU-bounded cache keyed by ``geometry_ref``.

Backs RFC §2.4 (revised). Full polygons are cached in process memory rather
than persisted to ``.storage``: geometry is ephemeral, and sustained disk
writes on SD-card deployments during severe-weather outbreaks are the wrong
tradeoff. Entries do not survive restart — the next coordinator poll
re-populates the cache from upstream.

The byte budget is per config entry, and an entry evicts only its own refs
(issue #197). The store is one process-wide singleton and refs are namespaced
``{entry_id}:{provider}:{alert_id}`` (``normalize._geometry_ref``), so a
global LRU let one heavy entry — a worldwide GDACS scope stored 4.3 MB of
the old 5 MB total on 2026-09-06 — push a sibling entry's polygons out, and
the first sign was a card drawing an empty frame off a 404. The owner of a
ref is its first ``:``-separated segment.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Per-entry cap on serialized polygon bytes. Sized so the heaviest entry
# measured to date (GDACS worldwide at the default Orange floor, 1.65 MB
# after #195, most of it drought outlines) fits three times over. Parsed
# dicts cost several times their JSON length in RAM, so raising this is not
# free on the Pi-class hardware much of the install base runs on.
MAX_BYTES = 5_000_000


def _owner(ref: str) -> str:
    """The entry a ref belongs to: everything before the first ``:``."""
    return ref.split(":", 1)[0]


class GeometryStore:
    """In-memory, LRU-bounded keyed geometry store."""

    def __init__(self, max_bytes: int | None = None) -> None:
        self._max_bytes = MAX_BYTES if max_bytes is None else max_bytes
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._owner_bytes: dict[str, int] = {}
        # Owners whose last put had to evict, so a sustained overflow logs
        # once at warning and then at debug until a put fits again.
        self._over_budget: set[str] = set()

    @property
    def max_bytes(self) -> int:
        """The per-entry budget in serialized bytes."""
        return self._max_bytes

    async def put(self, ref: str, geometry: dict[str, Any]) -> None:
        """Insert or update a geometry, enforcing the owner's LRU byte cap."""
        if ref in self._entries:
            self._drop(ref)
        self._insert(ref, geometry)
        self._evict_owner_to_cap(_owner(ref), keep=ref)

    async def get(self, ref: str) -> dict[str, Any] | None:
        """Return the geometry for ``ref`` or None. Promotes on read."""
        geom = self._entries.get(ref)
        if geom is None:
            return None
        self._entries.move_to_end(ref)
        return geom

    def has(self, ref: str) -> bool:
        """Whether ``ref`` is stored. No promotion."""
        return ref in self._entries

    def usage(self, owner: str) -> tuple[int, int]:
        """``(refs, bytes)`` stored for ``owner``."""
        refs = sum(1 for ref in self._entries if _owner(ref) == owner)
        return refs, self._owner_bytes.get(owner, 0)

    async def delete(self, ref: str) -> None:
        """Drop a single ref. No-op if absent."""
        if ref in self._entries:
            self._drop(ref)

    async def purge_missing(self, refs: set[str], prefix: str | None = None) -> None:
        """Drop stored refs not in ``refs``.

        If ``prefix`` is given, only refs starting with it are considered —
        used so one entry's purge doesn't nuke a sibling entry's geometry.
        """
        for ref in list(self._entries):
            if prefix is not None and not ref.startswith(prefix):
                continue
            if ref in refs:
                continue
            self._drop(ref)

    # -- internals --

    def _insert(self, ref: str, geometry: dict[str, Any]) -> None:
        self._entries[ref] = geometry
        size = len(json.dumps(geometry, separators=(",", ":")))
        self._sizes[ref] = size
        owner = _owner(ref)
        self._owner_bytes[owner] = self._owner_bytes.get(owner, 0) + size

    def _drop(self, ref: str) -> None:
        self._entries.pop(ref, None)
        size = self._sizes.pop(ref, 0)
        owner = _owner(ref)
        remaining = self._owner_bytes.get(owner, 0) - size
        if remaining > 0:
            self._owner_bytes[owner] = remaining
        else:
            self._owner_bytes.pop(owner, None)

    def _evict_owner_to_cap(self, owner: str, *, keep: str) -> None:
        """Evict ``owner``'s oldest refs until it fits, never ``keep``.

        The ref just written stays even when it alone is over the budget:
        a put that silently stored nothing would be worse than one polygon
        over the line.
        """
        evicted = 0
        evicted_bytes = 0
        while self._owner_bytes.get(owner, 0) > self._max_bytes:
            oldest = next(
                (r for r in self._entries if r != keep and _owner(r) == owner),
                None,
            )
            if oldest is None:
                break
            evicted += 1
            evicted_bytes += self._sizes.get(oldest, 0)
            self._drop(oldest)
        if not evicted:
            self._over_budget.discard(owner)
            return
        log = _LOGGER.debug if owner in self._over_budget else _LOGGER.warning
        self._over_budget.add(owner)
        log(
            "Geometry store: entry %s is over its %d byte polygon budget; "
            "evicted its %d oldest (%d bytes). Their entities keep bbox and "
            "geometry_ref but the card cannot draw them. Fewer or lighter "
            "alerts on this entry helps, such as a higher GDACS alert level",
            owner,
            self._max_bytes,
            evicted,
            evicted_bytes,
        )
