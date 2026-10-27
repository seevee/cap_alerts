"""DataUpdateCoordinator for CAP Alerts."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_COUNTRY,
    CONF_COUNTRY_ATTRIBUTE,
    CONF_COUNTRY_ENTITY,
    CONF_EXCLUDE_MARINE,
    CONF_GEOCODE_PREFIXES,
    CONF_GPS_LOC,
    CONF_LANGUAGE,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_TRACKER_ENTITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .conventions import conventions_for, register_source
from .geometry_store import GeometryStore
from .model import CAPAlert
from .normalize import normalize_alerts
from .providers import (
    AlertProvider,
    ProviderError,
    PushIngest,
    ScopeUnresolvedError,
    StreamingProvider,
)
from .providers.cap_content_cache import CAPContentCache
from .restore import AlertRestoreStore, Records
from .store import AlertStore, record_from_alert

_LOGGER = logging.getLogger(__name__)


def exclude_marine_alerts(alerts: list[CAPAlert], enabled: bool) -> list[CAPAlert]:
    """Drop marine/water-zone alerts when the exclude-marine option is on.

    Provider-neutral: relies on the per-provider ``is_marine`` flag. Returns
    the list unchanged when disabled (default), so non-marine-aware providers
    (MeteoAlarm, WMO) are unaffected.
    """
    if not enabled:
        return alerts
    return [a for a in alerts if not a.is_marine]


def matches_geocode_prefixes(alert: CAPAlert, prefixes: Sequence[str]) -> bool:
    """Whether any of the alert's area geocodes starts with any of ``prefixes``.

    Scheme-agnostic: every value in the ``geocodes`` container is compared,
    whatever its CAP ``valueName``. There is no cross-provider scheme-priority
    registry to derive a single "the" code from (MeteoAlarm's is country-scoped
    and WMO sources publish arbitrary schemes), and a colliding prefix
    over-matches — keeping extra alerts — rather than dropping wanted ones.

    Comparison is casefolded and whitespace-stripped on both sides, so
    alphabetic schemes (``UGC``, ``EMMA_ID``) behave like the numeric ones.
    ``prefixes`` is expected pre-folded by the caller.
    """
    for codes in alert.geocodes.values():
        for code in codes:
            folded = code.strip().casefold()
            if folded and any(folded.startswith(p) for p in prefixes):
                return True
    return False


def filter_by_geocode_prefixes(
    alerts: list[CAPAlert], prefixes: Sequence[str]
) -> list[CAPAlert]:
    """Keep alerts whose area geocodes match one of the configured prefixes.

    Provider-neutral narrowing on top of whatever location mode the entry uses:
    every provider populates ``CAPAlert.geocodes``, so this composes with NWS
    zones, ECCC provinces, MeteoAlarm regions, and WMO country-wide/GPS modes
    alike. Returns the list unchanged when no prefixes are configured (the
    default), so existing entries are untouched.

    A prefix is a *scope* because area codes are hierarchical: ``13`` is Hebei,
    ``1307`` Zhangjiakou, ``130709000000`` Chongli. Codes vary in length within
    one scheme — of 488 sampled CMA codes on 2026-08-04, 481 were 12 characters
    and 7 were 6 — so the match is a plain ``startswith`` with no zero-padding
    in either direction.

    Fails loud only on a *source capability* failure: the feed returned alerts
    but not one of them carries any geocode at all, so prefix filtering cannot
    work here (the parallel of "this source publishes no per-alert geometry" in
    the GPS filters). Zero *matches* is not a failure — "no alerts in my area"
    is the normal steady state, and failing there would leave the entry
    unavailable most of the time, inverting what unavailability means. The
    caller logs a one-shot warning for that case instead, which is where a
    typo'd prefix surfaces.
    """
    wanted = tuple(p.strip().casefold() for p in prefixes if p and p.strip())
    if not wanted or not alerts:
        return alerts
    if not any(a.geocodes for a in alerts):
        raise UpdateFailed(
            f"geocode filter requested but none of {len(alerts)} alerts carry "
            "area geocodes; this source does not publish them"
        )
    return [a for a in alerts if matches_geocode_prefixes(a, wanted)]


def _scope_key(config: Mapping[str, Any], options: Mapping[str, Any]) -> str:
    """A stable identity for the query a reconciliation runs.

    Serialized rather than hashed so unhashable option values (the geocode
    prefix list) participate without special-casing; ``default=str`` keeps an
    exotic value from raising here, where the cost of a wrong answer is one
    suspended retention cycle.
    """
    return json.dumps([config, options], sort_keys=True, default=str)


def device_name(provider_id: str) -> str:
    """The name of the device an entry's entities hang off, from its provider id.

    Module-level so the logbook platform can name a row the way the entity
    is named without a coordinator in hand: the entry may be gone by the time
    the row is read.
    """
    return f"CAP Alerts {provider_id.upper()}"


def _alert_unique_id(entry_id: str, provider: str, alert_id: str) -> str:
    """The unique_id ``sensor.AlertEntity`` registers an alert under."""
    return f"{entry_id}_{provider}_{alert_id}"


def known_alert_entities(
    hass: HomeAssistant, entry_id: str, provider: str
) -> dict[str, str]:
    """Alert id → registered name for every alert entity this entry already has.

    What the store treats as known before the boot (issue #250): the registry
    survives a restart where the store's memory does not, and the ids under the
    entry's alert prefix are exactly the set that had entities. The name is the
    entity's ``original_name``, which is the alert's ``event`` — all the
    registry keeps of the content.
    """
    alert_prefix = _alert_unique_id(entry_id, provider, "")
    return {
        ent.unique_id.removeprefix(alert_prefix): ent.original_name or ""
        for ent in er.async_entries_for_config_entry(er.async_get(hass), entry_id)
        if ent.unique_id.startswith(alert_prefix)
    }


def alert_entity_lookup(
    hass: HomeAssistant, entry_id: str, provider: str
) -> Callable[[str], str | None]:
    """A lookup from alert id to its registered ``entity_id``, or None."""

    def lookup(alert_id: str) -> str | None:
        return er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, _alert_unique_id(entry_id, provider, alert_id)
        )

    return lookup


def _records(data: Mapping[str, CAPAlert] | None) -> Records:
    """The restore file's view of an active set: id → persisted fields."""
    return {alert_id: record_from_alert(a) for alert_id, a in (data or {}).items()}


def _resolve_tracker_gps(state: Any) -> str | None:
    """Resolve a ``device_tracker`` state to a ``"lat,lon"`` string.

    Returns ``None`` when the state is missing or carries no usable
    coordinates. Latitude/longitude of exactly ``0.0`` is valid — only a
    truly absent attribute (``None``) is treated as unresolvable, so the
    equator/prime-meridian are not silently dropped.
    """
    if state is None:
        return None
    lat = state.attributes.get(ATTR_LATITUDE)
    lon = state.attributes.get(ATTR_LONGITUDE)
    if lat is None or lon is None:
        return None
    return f"{lat},{lon}"


class AlertsDataUpdateCoordinator(DataUpdateCoordinator[dict[str, CAPAlert]]):
    """Coordinator that delegates fetching to a provider."""

    config_entry: ConfigEntry

    # Identity of the query the last reconciliation ran, and whether the current
    # one differs (see ``_resolve_config``). Class-level so every construction
    # path has them, including the ``object.__new__`` shortcut the resolution
    # tests use to skip the coordinator's Home Assistant dependencies.
    _scope_key: str | None = None
    _scope_changed: bool = False

    # What the last resolution produced — tracker → coordinates, country entity
    # → ISO-2, language "auto" → a concrete tag. Diagnostics reports this pair
    # rather than re-running the resolution, which owns the scope key above and
    # would consume a scope change the next real cycle needs to see. Class-level
    # for the same reason as the two above.
    _resolved_config: dict[str, Any] | None = None
    _resolved_options: dict[str, Any] | None = None

    # The cycle-context bookkeeping (issue #310), class-level for the same
    # reason. ``_cycle_context`` itself is per instance, see ``__init__``.
    _pending_context: Context | None = None
    _cycle_origin: Context | None = None
    _cycle_settled: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        provider: AlertProvider,
        user_agent: str,
        geometry_store: GeometryStore,
        cap_content_cache: CAPContentCache | None = None,
        *,
        restore_store: AlertRestoreStore | None = None,
    ) -> None:
        self._provider = provider
        # The source's declared rows go into force before anything normalizes
        # one of its alerts; a second entry on the same provider re-registers
        # the same objects, which is a no-op.
        register_source(provider.conventions)
        # The context the current cycle's events and state writes carry, so
        # Activity can trace one to the other (issue #310). ``_begin_cycle``
        # replaces it whenever listeners are about to be told something new.
        self._cycle_context = Context()
        self._store = AlertStore(
            entry.entry_id,
            provider.name,
            fire=self._async_fire_event,
            lookup_entity_id=alert_entity_lookup(hass, entry.entry_id, provider.name),
            # Read here rather than borrowed from the sensor platform, which
            # hydrates only after the first refresh has already run.
            known_at_boot=known_alert_entities(hass, entry.entry_id, provider.name),
            defer_until_registered=True,
        )
        self._geometry_store = geometry_store
        self._user_agent = user_agent
        self._cap_content_cache = cap_content_cache
        # Per-entry persistence of the live set's lifecycle fields (issue
        # #350). ``None`` means no persistence, which is what a coordinator
        # built directly in a test gets.
        self._restore_store = restore_store
        # When the last reconciliation ran, from any path. Stamped as the
        # restore file's ``confirmed_at``; ``None`` until one has run.
        self._last_reconciled_at: datetime | None = None
        self._timeout: int = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        # Snapshot of the entry data this coordinator was built from. The
        # reconfigure flow no longer reloads the entry itself (see
        # __init__._async_entry_updated), so this is what tells the update
        # listener that a rebuild is required rather than an in-place tweak.
        self._entry_data = dict(entry.data)
        self.last_update_success_time: datetime | None = None
        # When the alert set last changed: a reconciliation that fired or
        # parked an incident event, from any path (issue #323). What the
        # last-updated sensor reports; the sensor seeds it from its restored
        # state at boot, since the store starts empty.
        self.last_change_time: datetime | None = None
        # Last failed update, kept *after* a recovery rather than cleared: the
        # diagnostics dump is read after the fact, and "it broke at 04:12 and
        # has been fine since" is the answer a report needs. Compare against
        # ``last_update_success_time`` to tell which came last.
        self.last_update_failure_time: datetime | None = None
        self.last_update_failure: str | None = None
        # Guard a single warning per failure streak when a tracker or
        # country-source entity can't be resolved, so the per-poll resolution
        # doesn't spam the log.
        self._tracker_resolve_warned = False
        self._country_resolve_warned = False
        # Same guard for a geocode-prefix filter that matches nothing — the
        # only signal distinguishing a typo'd prefix from a quiet period.
        self._geocode_no_match_warned = False

        # Real-time ingestion, for a provider that offers it and an entry that
        # asks for it. The ingest owns its transport and its document set; this
        # coordinator starts it, stops it, runs its backfill on the resync
        # cadence, and hands it the shared pipeline through ``IngestHost``.
        self._ingest: PushIngest | None = None
        if isinstance(provider, StreamingProvider) and provider.streaming_enabled(
            entry.options
        ):
            self._ingest = provider.build_ingest(self)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=self.resolve_update_interval(entry),
        )

    def streaming_enabled(self, entry: ConfigEntry) -> bool:
        """Whether ``entry`` asks the provider for real-time ingestion.

        Derived from the entry rather than this coordinator's state, so the
        update listener can compare a *pending* options change against a live
        coordinator's ``streaming``.
        """
        return isinstance(
            self._provider, StreamingProvider
        ) and self._provider.streaming_enabled(entry.options)

    def entry_data_changed(self, entry: ConfigEntry) -> bool:
        """Whether ``entry`` data differs from what this coordinator was built from.

        Provider, location, source id, and filter mode are all read once at
        construction, so a reconfigure that rewrites them needs a rebuild — the
        update listener's cue to reload. Compares the whole mapping rather than
        named keys so a newly added data key cannot silently skip the reload.
        """
        return dict(entry.data) != self._entry_data

    @property
    def streaming(self) -> bool:
        """Whether this coordinator ingests in real time rather than polling."""
        return self._ingest is not None

    @property
    def ingest(self) -> PushIngest | None:
        """The provider's push ingest, or None for a polling entry."""
        return self._ingest

    @property
    def stream_connected(self) -> bool:
        """Whether the ingest's socket is currently established.

        Always ``False`` for a polling entry. Distinct from availability: the
        socket can be down while the entry is perfectly healthy on backfills,
        which is precisely the state the connectivity entity exists to surface.
        """
        return self._ingest is not None and self._ingest.connected

    @property
    def ingest_diagnostics(self) -> dict[str, Any] | None:
        """The ingest's own facts for the diagnostics dump, or None when polling."""
        return self._ingest.diagnostics() if self._ingest is not None else None

    def resolve_update_interval(self, entry: ConfigEntry) -> timedelta:
        """Poll interval: the scan interval, or the ingest's resync cadence.

        Public because the options-update listener re-derives the interval from a
        changed entry, and the streaming-vs-polling branch must not be duplicated
        there.
        """
        if self._ingest is not None:
            return self._ingest.resync_interval
        return timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

    @property
    def provider(self) -> AlertProvider:
        """Expose provider for device_info model field."""
        return self._provider

    @property
    def device_info(self) -> DeviceInfo:
        """Device identity for every entity of this config entry.

        Single source of truth: the sensor and button platforms both defer here,
        so the device name/model cannot drift between them — a mismatch would
        split one entry's entities across two devices in the registry.
        """
        return DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name=device_name(self._provider.name),
            manufacturer="CAP Alerts",
            model=self._provider.name.upper(),
        )

    def update_timeout(self, timeout: int) -> None:
        """Called by options update listener."""
        self._timeout = timeout

    def _resolve_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve config and options before passing to provider.

        - Tracker mode: resolves tracker entity -> lat/lon coordinates.
        - Country-source mode: resolves a country entity -> the source's
          country code, through the row's ``country_code`` hook.
        - Language "auto": resolves to a concrete tag, through the row's
          ``resolve_language`` hook; a source without one takes no language
          and ``auto`` is left in place.
        """
        config = dict(self.config_entry.data)
        options = dict(self.config_entry.options)
        # The provider row: resolution runs before any sender is known.
        conventions = conventions_for(config.get(CONF_PROVIDER, ""))

        # Resolve tracker entity -> GPS coordinates. An unresolvable tracker
        # (missing state or no lat/lon) raises UpdateFailed so the entry goes
        # visibly unavailable rather than silently degrading to zero or
        # country-wide alerts.
        if CONF_TRACKER_ENTITY in config:
            entity_id = config[CONF_TRACKER_ENTITY]
            gps = _resolve_tracker_gps(self.hass.states.get(entity_id))
            if gps is None:
                provider = config.get(CONF_PROVIDER, "")
                if not self._tracker_resolve_warned:
                    _LOGGER.warning(
                        "%s: tracker %s has no location", provider, entity_id
                    )
                    self._tracker_resolve_warned = True
                raise UpdateFailed(f"{provider}: tracker {entity_id} has no location")
            self._tracker_resolve_warned = False
            config[CONF_GPS_LOC] = gps

        # Resolve country-source entity -> country code (fully-mobile mode).
        # Leaving CONF_COUNTRY unset lets the provider's existing "country not
        # configured" path surface UpdateFailed.
        if CONF_COUNTRY_ENTITY in config and conventions.country_code is not None:
            entity_id = config[CONF_COUNTRY_ENTITY]
            state = self.hass.states.get(entity_id)
            value: str | None = None
            if state is not None and state.state not in (
                "",
                "unknown",
                "unavailable",
            ):
                attr = config.get(CONF_COUNTRY_ATTRIBUTE)
                value = state.attributes.get(attr) if attr else state.state
            code = conventions.country_code(value)
            if code is None:
                if not self._country_resolve_warned:
                    _LOGGER.warning(
                        "%s: could not resolve country from %s (value=%r)",
                        config.get(CONF_PROVIDER, ""),
                        entity_id,
                        value,
                    )
                    self._country_resolve_warned = True
            else:
                self._country_resolve_warned = False
                config[CONF_COUNTRY] = code

        # Resolve language "auto" -> a concrete tag, the way the source's row
        # says: MeteoAlarm wants a 2-letter prefix, WMO and BBK the full tag,
        # ECCC one of its two. A source with no hook takes no language.
        lang = options.get(CONF_LANGUAGE, "auto")
        if lang == "auto" and conventions.resolve_language is not None:
            options[CONF_LANGUAGE] = conventions.resolve_language(
                self.hass.config.language
            )

        # Compared against the previous cycle's, this decides whether the store
        # may retain alerts missing from the incoming set. Taken after
        # resolution so a moved tracker registers, and over the whole resolved
        # pair so a reconfigured zone, a flipped marine toggle, or narrowed
        # geocode prefixes register too: each of those makes the previous active
        # set answer a question no longer being asked, and none of them is a
        # feed dropout. Options that do not narrow the query cost at most one
        # cycle of suspended retention when they change.
        key = _scope_key(config, options)
        self._scope_changed = self._scope_key is not None and key != self._scope_key
        self._scope_key = key

        self._resolved_config = config
        self._resolved_options = options
        return config, options

    @property
    def geometry_store(self) -> GeometryStore:
        """The shared polygon store, for diagnostics."""
        return self._geometry_store

    @callback
    def async_release_events(self, alert_id: str, entity_id: str) -> None:
        """Let the store fire the events it parked for a newly added alert entity.

        ``store.process`` runs inside the refresh and the sensor platform adds
        the alert's entity afterwards, so a first sighting's events wait here
        for the entity_id the platform assigned (issue #249). The entity calls
        this from ``async_added_to_hass``.
        """
        self._store.release(alert_id, entity_id)

    @property
    def boot_pending_ids(self) -> frozenset[str]:
        """Alert ids known before the restart that no fetch has settled yet."""
        return self._store.boot_pending

    @property
    def seeded_at_boot(self) -> int:
        """How many alerts the restore file seeded at setup (issue #350)."""
        return self._store.seeded_at_boot

    @property
    def last_saved(self) -> str | None:
        """When the restore file was last written or scheduled, ISO 8601."""
        if self._restore_store is None:
            return None
        return self._restore_store.last_saved

    async def async_restore(self) -> None:
        """Seed the store from the fields the last run saved (issue #350).

        Runs before the first refresh so that refresh reconciles against the
        restored records: one the feed omits is retained to its expiry rather
        than removed as a registry-only id, and one that ended while HA was
        down is removed with its severity and area. The saved scope key goes
        in with it, so a scope that changed across the restart (a reconfigure
        reloads the entry, a tracker moves) still suspends retention on that
        first cycle and the out-of-scope alerts are removed rather than
        stranded.

        Fires nothing, and safe to repeat: a failed first refresh makes HA
        retry setup, which builds a new coordinator and calls this again.
        """
        if self._restore_store is None:
            return
        loaded = await self._restore_store.async_load()
        if loaded is None:
            return
        self._scope_key = loaded.scope_key
        self._store.restore(loaded.records, confirmed_at=loaded.confirmed_at)

    async def async_save_restore_state(self) -> None:
        """Write the live set now, for unload and HA stop.

        Refreshes ``confirmed_at`` to the last reconciliation, which a
        write-on-change file would otherwise leave at the last change. Skipped
        when nothing has reconciled since setup: there is nothing newer to say.
        """
        if self._restore_store is None or self._last_reconciled_at is None:
            return
        await self._restore_store.async_save_now(
            _records(self.data),
            scope_key=self._scope_key,
            confirmed_at=self._last_reconciled_at.isoformat(),
        )

    @property
    def resolved_config(self) -> Mapping[str, Any]:
        """Entry data as the last resolution left it, or the raw data before one."""
        if self._resolved_config is None:
            return self.config_entry.data
        return self._resolved_config

    @property
    def resolved_options(self) -> Mapping[str, Any]:
        """Entry options as the last resolution left it, or the raw options."""
        if self._resolved_options is None:
            return self.config_entry.options
        return self._resolved_options

    # ------------------------------------------------------------------
    # IngestHost: what a push ingest may ask of this coordinator
    # ------------------------------------------------------------------

    @property
    def entry_id(self) -> str:
        return self.config_entry.entry_id

    @property
    def timeout(self) -> int:
        return self._timeout

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @property
    def cap_content_cache(self) -> CAPContentCache | None:
        return self._cap_content_cache

    def resolve_scope(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """``_resolve_config`` for the ingest, in the provider layer's exception."""
        try:
            return self._resolve_config()
        except UpdateFailed as err:
            raise ScopeUnresolvedError(str(err)) from err

    @callback
    def push(self, data: dict[str, CAPAlert]) -> None:
        """Publish stream-sourced data to entities.

        Deliberately *not* ``async_set_updated_data``: that resets the
        ``update_interval`` timer, so heartbeats arriving every ~60 s would defer
        the safety-resync backfill indefinitely and it would never run. It also
        asserts ``last_update_success``, which would let a heartbeat mark
        entities available again while the authoritative backfill is failing.
        Only a backfill drives availability (issue #16); the stream publishes
        data and notifies listeners, nothing more.
        """
        self.data = data
        self.async_update_listeners()

    @callback
    def notify_connection(self, connected: bool) -> None:
        """A socket connect/disconnect: notify listeners, touch nothing else.

        It must not touch ``last_update_success``: a dropped socket is not a
        failed data refresh (issue #16), and the entry stays available on
        backfills while the client reconnects. It is its own cycle, though: a
        write it causes must not be traced to the last reconciliation's events.
        """
        self._begin_cycle()
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # Cycle context (issue #310)
    # ------------------------------------------------------------------

    @property
    def cycle_context(self) -> Context:
        """The context an entity writes under for the cycle being published.

        Entities set it right before they write rather than when the cycle
        began: Home Assistant honours a context set on an entity within the
        last 5 s, and a slow fetch takes longer than that.
        """
        return self._cycle_context

    @callback
    def _begin_cycle(self) -> None:
        """Start a cycle: a fresh context, or the one a user's refresh parked.

        ``_cycle_origin`` keeps the user's context apart from the cycle's own
        because an event fired after the writes gets a context of its own and
        this is the parent it is given; ``_cycle_settled`` is whether the
        cycle's listeners have run, which is what decides that (see
        ``_async_fire_event``).
        """
        pending, self._pending_context = self._pending_context, None
        self._cycle_context = pending or Context()
        self._cycle_origin = pending
        self._cycle_settled = False

    @callback
    def async_update_listeners(self) -> None:
        """Mark the cycle's writes as done before the base class runs them."""
        self._cycle_settled = True
        super().async_update_listeners()

    async def async_request_refresh(self, context: Context | None = None) -> None:
        """Request a refresh whose cycle carries ``context`` (issue #310).

        The refresh button arrives with the user's context on the entity, but
        the base method goes through the debouncer and the cycle it starts
        would otherwise mint an anonymous one. Parked here and adopted by the
        next cycle that begins, so Activity reads the writes and events of
        that cycle as triggered by the press.
        """
        if context is not None:
            self._pending_context = context
        await super().async_request_refresh()

    @callback
    def _async_fire_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """The store's ``fire``: one bus event, under a context Activity can trace.

        Logbook attributes a row to the first row it saw with the same context,
        hidden rows included. An event fired before the cycle's entities write
        (``incident_updated`` and ``incident_removed`` fire from the diff)
        shares the cycle's context, so the last-updated row that follows reads
        as caused by it. An event fired *after* the writes, which is every
        event the store parks until its entity exists (``incident_created``,
        issue #249), gets a context of its own: shared, one of those writes
        would come first and the alert's issue would read as caused by it.
        When the cycle was a user's refresh the child names that context as
        its parent, so the row still traces to the press.
        """
        context = self._cycle_context
        if self._cycle_settled:
            origin = self._cycle_origin
            context = Context(parent_id=origin.id if origin is not None else None)
        self.hass.bus.async_fire(event_type, payload, context=context)

    async def _async_update_data(self) -> dict[str, CAPAlert]:
        try:
            return await self._async_fetch_data()
        except Exception as err:
            # Recorded for diagnostics, then re-raised untouched — the base
            # coordinator still owns availability, logging, and backoff.
            self.last_update_failure = str(err) or type(err).__name__
            self.last_update_failure_time = datetime.now(timezone.utc)
            # The unavailable writes that follow are this failure's, not the
            # last successful cycle's.
            self._begin_cycle()
            raise

    async def _async_fetch_data(self) -> dict[str, CAPAlert]:
        config, options = self._resolve_config()

        # Under real-time ingestion the periodic tick is the safety-resync
        # backfill, not the primary ingestion path (the ingest pushes as
        # documents arrive). It is still the fetch that drives availability.
        try:
            if self._ingest is not None:
                data = await self._ingest.async_backfill(config, options)
            else:
                alerts = await self._fetch(config, options)
                data = await self.async_apply(alerts, fetched=True)
        except ProviderError as err:
            # The one place the provider layer's exception becomes Home
            # Assistant's. The message is passed through untouched, so
            # ``last_update_failure`` and the log read as the provider wrote
            # them.
            raise UpdateFailed(str(err)) from err
        self.last_update_success_time = datetime.now(timezone.utc)
        return data

    async def _fetch(
        self, config: Mapping[str, Any], options: Mapping[str, Any]
    ) -> list[CAPAlert]:
        """One provider poll under the entry timeout, in the provider's exception."""
        try:
            async with asyncio.timeout(self._timeout):
                return await self._provider.async_fetch(
                    async_get_clientsession(self.hass),
                    config,
                    options,
                    cap_content_cache=self._cap_content_cache,
                    user_agent=self._user_agent,
                )
        except TimeoutError as err:
            raise ProviderError(
                f"{self._provider.name}: timeout after {self._timeout}s"
            ) from err
        except aiohttp.ClientResponseError as err:
            # ``str(err)`` quotes the request URL, which in NWS GPS and tracker
            # modes carries the coordinates. The status and reason say what
            # happened; the endpoint is in diagnostics already, redacted.
            raise ProviderError(
                f"{self._provider.name}: HTTP {err.status} {err.message}"
            ) from err
        except aiohttp.ClientError as err:
            raise ProviderError(f"{self._provider.name}: {err}") from err

    async def async_apply(
        self, alerts: list[CAPAlert], *, fetched: bool
    ) -> dict[str, CAPAlert]:
        """Run the shared post-fetch pipeline and index the active set by ID.

        Normalize → marine filter → geocode filter → geometry externalization →
        store diff. Used by the polling path and by a push ingest's rebuild
        and backfill paths alike, so their transition detection, event firing,
        and geometry handling are identical.

        Deliberately does *not* stamp ``last_update_success_time``: a push
        ingest runs this pipeline on every heartbeat with no network I/O, and
        diagnostics report when data was last *fetched*. Only the fetch-backed
        callers stamp it, and only they set ``fetched``, which is what the
        store's boot grace counts (issue #252). ``last_change_time`` is stamped
        here instead, on any path: a heartbeat rebuild that issues or ends an
        alert has changed the set as surely as a poll has (issue #323).
        """
        # Shared normalization. The full normalized list — including
        # cancelled/expired alerts — is handed to store.process so it can
        # fire incident_removed with the true terminal phase before
        # dropping them from the active set (RFC §2.3).
        self._begin_cycle()
        entry_id = self.config_entry.entry_id
        alerts = normalize_alerts(alerts, entry_id)
        # Opt-in marine filter (NWS/ECCC). Dropped alerts flow through store as
        # silent disappearances, so existing marine entities are removed (firing
        # incident_removed) when the toggle is flipped on.
        exclude_marine = self.config_entry.options.get(CONF_EXCLUDE_MARINE, False)
        alerts = exclude_marine_alerts(alerts, exclude_marine)
        # Opt-in area-code narrowing, layered on the entry's location mode.
        prefixes = self.config_entry.options.get(CONF_GEOCODE_PREFIXES) or []
        if prefixes:
            kept = filter_by_geocode_prefixes(alerts, prefixes)
            self._warn_geocode_no_match(alerts, kept, prefixes)
            alerts = kept
        # Externalize geometry for alerts that will remain active. Skipping
        # terminal-phase alerts avoids caching polygons we're about to drop.
        active_refs: set[str] = set()
        for a in alerts:
            if a.phase in ("cancel", "expired"):
                continue
            if a.geometry_ref and a.geometry:
                await self._geometry_store.put(a.geometry_ref, a.geometry)
                active_refs.add(a.geometry_ref)
        # Purge only this entry's refs (geometry_ref is entry-namespaced), so a
        # sibling entry on the same provider keeps its geometry.
        await self._geometry_store.purge_missing(active_refs, prefix=f"{entry_id}:")
        # Diff against previous poll — returns only active alerts.
        alerts = self._store.process(
            alerts,
            scope_changed=self._scope_changed,
            superseded_identifiers=(
                self._ingest.superseded_identifiers
                if self._ingest is not None
                else frozenset()
            ),
            fetched=fetched,
        )
        self._scope_changed = False
        now = datetime.now(timezone.utc)
        self._last_reconciled_at = now
        active = {a.id: a for a in alerts}
        if self._store.transitions:
            self.last_change_time = now
            if self._restore_store is not None:
                # Written on change only (RFC §1.4 requirement 5), and the
                # store's transition count is the change signal: every field
                # the record keeps is on the ``changed_fields`` allowlist, so
                # a cycle that moved one fired an event. The file compares
                # the records once more, so a content-only update costs no
                # write either.
                self._restore_store.async_save_if_changed(
                    _records(active),
                    scope_key=self._scope_key,
                    confirmed_at=now.isoformat(),
                )
        return active

    def _warn_geocode_no_match(
        self,
        before: list[CAPAlert],
        after: list[CAPAlert],
        prefixes: Sequence[str],
    ) -> None:
        """Warn once when a geocode prefix filters everything out.

        The filter deliberately does not fail on zero matches, which leaves a
        mistyped prefix indistinguishable from a genuinely quiet area — the
        entry stays healthy and simply reports no alerts, forever. One WARNING
        per no-match streak surfaces that without flapping entity availability;
        the flag resets on the first poll that matches something.
        """
        if after:
            self._geocode_no_match_warned = False
            return
        if not before or self._geocode_no_match_warned:
            return
        self._geocode_no_match_warned = True
        _LOGGER.warning(
            "%s: geocode prefixes %s matched none of %d alerts. Alerts are "
            "published for codes such as %s — check the prefix, and note that "
            "a shorter prefix covers a wider area",
            self._provider.name,
            ",".join(prefixes),
            len(before),
            ", ".join(sorted(self._sample_geocodes(before))) or "(none)",
        )

    @staticmethod
    def _sample_geocodes(alerts: list[CAPAlert], limit: int = 5) -> set[str]:
        """A few distinct area codes from ``alerts``, to make the warning actionable."""
        sample: set[str] = set()
        for alert in alerts:
            for codes in alert.geocodes.values():
                for code in codes:
                    sample.add(code)
                    if len(sample) >= limit:
                        return sample
        return sample

    # ------------------------------------------------------------------
    # Ingest lifecycle
    # ------------------------------------------------------------------

    async def async_start_stream(self) -> None:
        """Start the provider's push ingest, if this entry has one. Idempotent."""
        if self._ingest is not None:
            await self._ingest.async_start()

    async def async_stop_stream(self) -> None:
        """Stop the push ingest, if any. Idempotent; no task leak."""
        if self._ingest is not None:
            await self._ingest.async_stop()
