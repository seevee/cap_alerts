"""Alert provider protocol and factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import aiohttp

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from ..conventions import SourceConventions
from ..model import CAPAlert
from .cap_content_cache import CAPContentCache

# Every shipped provider id, in the order the setup menu offers them.
PROVIDER_IDS: tuple[str, ...] = (
    "nws",
    "eccc",
    "meteoalarm",
    "wmo",
    "gdacs",
    "bbk",
    "au",
)


class ProviderError(Exception):
    """A fetch that could not produce alerts this cycle.

    Raised by every provider for transient failures — network errors, a non-200
    status, a body that does not parse — and translated into Home Assistant's
    ``UpdateFailed`` by the coordinator in one place. Providers never import
    that class: the exception is the one seam between the provider layer and
    the entity lifecycle, and keeping it ours is what lets the provider side be
    hosted by something other than this integration's coordinator.
    """


class ScopeUnresolvedError(ProviderError):
    """The host could not resolve the entry's scope this cycle.

    Raised by ``IngestHost.resolve_scope`` when a tracker has no location, say.
    A push ingest drops the push on it; the next backfill re-seeds from the
    authoritative feed.
    """


class AlertProvider(Protocol):
    """Fetches alerts from a single weather service and returns CAPAlert objects."""

    @property
    def name(self) -> str:
        """Provider identifier for CAPAlert.provider field (e.g. 'nws', 'eccc')."""
        ...

    @property
    def conventions(self) -> Mapping[str, SourceConventions]:
        """What this source declares about itself, keyed as ``conventions_for``
        resolves it: ``name`` for the provider row, ``name/sender`` for a
        dialect. The coordinator puts them into force when it builds the
        provider (``conventions.register_source``); the options flow reads
        them straight off the provider.
        """
        ...

    async def async_fetch(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        options: Mapping[str, Any],
        *,
        cap_content_cache: CAPContentCache | None = None,
        user_agent: str | None = None,
    ) -> list[CAPAlert]:
        """Fetch current alerts. Raises ProviderError on transient errors."""
        ...

    async def async_validate_config(
        self,
        session: aiohttp.ClientSession,
        config: Mapping[str, Any],
        *,
        user_agent: str | None = None,
    ) -> str | None:
        """Check a configured scope before an entry is created (issue #131).

        Returns a ``strings.json`` error key when the scope will never produce
        alerts, or ``None`` when it is usable. Callers map the key onto the
        form the user is looking at.

        The question is whether *this scope resolves*, not whether the service
        is up: "is api.weather.gov reachable" is the coordinator's problem and
        already surfaces as an unavailable entity. Implementations answer with
        the cheapest authoritative request they have — never a full alert
        fetch — and return ``None`` for a mode they cannot check, so an
        unverifiable scope is created rather than blocked.
        """
        ...


class IngestHost(Protocol):
    """What a push ingest may ask of the coordinator that hosts it.

    The ingest owns the documents; the host owns the entry, the shared
    pipeline, and the entities. Everything the ingest needs from that side is
    named here so the ingest never reaches into the coordinator.
    """

    @property
    def hass(self) -> HomeAssistant: ...

    @property
    def entry_id(self) -> str: ...

    @property
    def timeout(self) -> int: ...

    @property
    def user_agent(self) -> str: ...

    @property
    def cap_content_cache(self) -> CAPContentCache | None: ...

    def resolve_scope(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The entry's resolved ``(config, options)``, or ``ScopeUnresolvedError``."""
        ...

    async def async_apply(
        self, alerts: list[CAPAlert], *, fetched: bool
    ) -> dict[str, CAPAlert]:
        """Run the shared pipeline over a rebuilt alert list; returns the active set."""
        ...

    def push(self, data: dict[str, CAPAlert]) -> None:
        """Publish a stream-sourced active set to entities without a fetch."""
        ...

    def notify_connection(self, connected: bool) -> None:
        """Tell entity listeners the socket connected or dropped."""
        ...


class PushIngest(Protocol):
    """A provider's real-time ingestion for one entry, as the coordinator holds it.

    The coordinator never sees the transport: it starts and stops the ingest,
    runs its backfill on the resync cadence, and reads a few facts off it.
    """

    @property
    def connected(self) -> bool: ...

    @property
    def resync_interval(self) -> timedelta: ...

    @property
    def superseded_identifiers(self) -> frozenset[str]: ...

    async def async_start(self) -> None: ...

    async def async_stop(self) -> None: ...

    async def async_backfill(
        self, config: Mapping[str, Any], options: Mapping[str, Any]
    ) -> dict[str, CAPAlert]:
        """The fetch-backed resync: seed the live set and return the active set.

        Raises ``ProviderError`` on fetch failure, which is what drives
        availability under streaming.
        """
        ...

    def diagnostics(self) -> dict[str, Any]:
        """JSON-ready facts for the ``stream`` block of the diagnostics dump."""
        ...


@runtime_checkable
class StreamingProvider(Protocol):
    """A provider that can ingest in real time instead of polling."""

    def streaming_enabled(self, options: Mapping[str, Any]) -> bool:
        """Whether the entry's options ask for real-time ingestion."""
        ...

    def build_ingest(self, host: IngestHost) -> PushIngest:
        """The ingest for one entry; the host hands it the shared pipeline."""
        ...


def get_provider(provider_id: str) -> AlertProvider:
    """Return a provider instance by ID."""
    from .au import AUProvider
    from .bbk import BBKProvider
    from .eccc import ECCCProvider
    from .gdacs import GDACSProvider
    from .meteoalarm import MeteoAlarmProvider
    from .nws import NWSProvider
    from .wmo import WMOProvider

    providers: dict[str, Callable[[], AlertProvider]] = {
        "nws": NWSProvider,
        "eccc": ECCCProvider,
        "meteoalarm": MeteoAlarmProvider,
        "wmo": WMOProvider,
        "gdacs": GDACSProvider,
        "bbk": BBKProvider,
        "au": AUProvider,
    }
    cls = providers.get(provider_id)
    if cls is None:
        raise ValueError(f"Unknown provider: {provider_id}")
    return cls()
