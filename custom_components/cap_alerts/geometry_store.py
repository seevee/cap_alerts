"""Geometry store — externalized GeoJSON polygons keyed by geometry_ref.

Backs RFC §2.4. Full polygons live in ``.storage/cap_alerts_geometry`` rather
than in entity attributes; consumers fetch them out-of-band via the REST view
or websocket command registered alongside this store.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "cap_alerts_geometry"
STORAGE_VERSION = 1

# Soft cap on total serialized polygon bytes. 5 MB ≈ 500 alerts × 10 KB.
MAX_BYTES = 5_000_000


class GeometryStore:
    """LRU-bounded, debounce-persisted keyed geometry store."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        self._loaded = False

    async def load_once(self) -> None:
        """Hydrate from ``.storage``. Safe to call repeatedly."""
        if self._loaded:
            return
        self._loaded = True
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001 — corrupt storage shouldn't block startup
            _LOGGER.exception("Failed to load geometry store; starting empty")
            data = None
        if not data:
            return
        for entry in data.get("entries", []):
            ref = entry.get("ref")
            geom = entry.get("geometry")
            if not ref or not isinstance(geom, dict):
                continue
            self._insert(ref, geom)

    async def put(self, ref: str, geometry: dict) -> None:
        """Insert or update a geometry, enforcing the LRU byte cap."""
        await self.load_once()
        if ref in self._entries:
            self._drop(ref)
        self._insert(ref, geometry)
        self._evict_to_cap()
        self._schedule_save()

    async def get(self, ref: str) -> dict | None:
        """Return the geometry for ``ref`` or None. Promotes on read."""
        await self.load_once()
        geom = self._entries.get(ref)
        if geom is None:
            return None
        self._entries.move_to_end(ref)
        return geom

    async def delete(self, ref: str) -> None:
        """Drop a single ref. No-op if absent."""
        await self.load_once()
        if ref in self._entries:
            self._drop(ref)
            self._schedule_save()

    async def purge_missing(self, refs: set[str], prefix: str | None = None) -> None:
        """Drop stored refs not in ``refs``.

        If ``prefix`` is given, only refs starting with it are considered —
        used so one entry's purge doesn't nuke a sibling entry's geometry.
        """
        await self.load_once()
        changed = False
        for ref in list(self._entries):
            if prefix is not None and not ref.startswith(prefix):
                continue
            if ref in refs:
                continue
            self._drop(ref)
            changed = True
        if changed:
            self._schedule_save()

    # -- internals --

    def _insert(self, ref: str, geometry: dict) -> None:
        self._entries[ref] = geometry
        size = len(json.dumps(geometry, separators=(",", ":")))
        self._sizes[ref] = size
        self._total_bytes += size

    def _drop(self, ref: str) -> None:
        self._entries.pop(ref, None)
        size = self._sizes.pop(ref, 0)
        self._total_bytes -= size

    def _evict_to_cap(self) -> None:
        while self._total_bytes > MAX_BYTES and self._entries:
            oldest = next(iter(self._entries))
            self._drop(oldest)

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._snapshot, 10)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "entries": [
                {"ref": ref, "geometry": geom} for ref, geom in self._entries.items()
            ]
        }
