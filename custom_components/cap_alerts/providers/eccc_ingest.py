"""ECCC push ingest: the NAAD socket, heartbeats, backfill and repository recovery.

This is the provider side of real-time ECCC: it owns the live document set the
socket feeds, decides what enters it, and asks the host to run the shared
pipeline whenever that set changes. The host — this integration's coordinator —
sees documents arriving and nothing of how. ``NAADStreamClient`` underneath
owns only the transport.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import client_context

from ..const import (
    CONF_LANGUAGE,
    CONF_PROVINCE,
    DEFAULT_STREAM_RESYNC_INTERVAL,
    DOMAIN,
    NAAD_REPOSITORY_FETCH_ATTEMPTS,
    NAAD_REPOSITORY_URL,
    NAAD_STREAM_BACKFILL_MIN_INTERVAL_S,
    NAAD_STREAM_HOST,
    NAAD_STREAM_PORT,
)
from ..conventions import parse_instant
from ..model import CAPAlert
from . import IngestHost, ProviderError, ScopeUnresolvedError
from .cap import CAPDoc, parse_cap_alert
from .eccc import (
    ECCCProvider,
    build_alerts_from_cap_docs,
    doc_matches_region,
    is_actual,
)
from .naad_stream import NAADStreamClient

_LOGGER = logging.getLogger(__name__)


def _sent_before(sent_text: str, cutoff: datetime) -> bool:
    """Whether a CAP ``sent`` timestamp string is before ``cutoff``.

    Fails open — an unparseable or missing ``sent`` returns ``False`` so the
    record is retained rather than pruned on a formatting quirk. A tz-naive
    timestamp is assumed UTC.
    """
    sent = parse_instant(sent_text)
    return sent is not None and sent < cutoff


class NAADIngest:
    """Real-time ECCC ingestion for one config entry (``PushIngest``).

    A background NAAD stream client pushes CAP docs into ``_live_docs`` and the
    GeoRSS feed is used only as (re)connect + periodic-resync backfill; the
    host's poll interval becomes the safety-resync cadence rather than the hot
    loop.
    """

    def __init__(self, host: IngestHost, provider: ECCCProvider) -> None:
        self._host = host
        self._provider = provider
        self._live_docs: dict[str, CAPDoc] = {}
        # Every CAP identifier this entry has laid eyes on, admitted or not,
        # mapped to its ``sent`` so it ages out on the same 48 h clock as the
        # live set. This is what a heartbeat's <references> are diffed against
        # (issue #164): the live set alone would make every out-of-region alert
        # in the country look unseen, and get it refetched once a minute until
        # it left the heartbeat's window.
        self._seen: dict[str, str] = {}
        # Failed repository fetches per identifier, for the give-up bound. Only
        # ever holds identifiers in the current heartbeat's window.
        self._repository_attempts: dict[str, int] = {}
        self._repository_recovered = 0
        self._ingest_lock = asyncio.Lock()
        self._stream_client: NAADStreamClient | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._connected = False
        # When the last GeoRSS backfill was attempted, from either source, so a
        # reconnect-triggered one can be throttled against it.
        self._last_backfill_at: datetime | None = None
        self._stream_backfill_warned = False

    # ------------------------------------------------------------------
    # What the host reads
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the NAAD socket is currently established.

        Distinct from availability: the socket can be down while the entry is
        perfectly healthy on backfills, which is precisely the state the
        connectivity entity exists to surface.
        """
        return self._connected

    @property
    def resync_interval(self) -> timedelta:
        """How often the host should run the safety-resync backfill."""
        return timedelta(seconds=DEFAULT_STREAM_RESYNC_INTERVAL)

    @property
    def superseded_identifiers(self) -> frozenset[str]:
        """Every CAP identifier a document in the live set references.

        A revision whose geometry moved off the user is screened out before the
        store sees it, so its ``references`` never reach the store's own view;
        the live set still holds them, and the alert it replaced has genuinely
        been replaced rather than gone unobserved.
        """
        return frozenset(
            ref_id
            for doc in self._live_docs.values()
            for _, ref_id, _ in doc.references
        )

    @property
    def repository_recovered(self) -> int:
        """How many CAP bodies heartbeat references have pulled from the repository.

        Counts fetches, not admissions: a recovered document can still be
        screened out by region. Since setup — it is not persisted.
        """
        return self._repository_recovered

    def diagnostics(self) -> dict[str, Any]:
        """The ``stream`` block of the config-entry diagnostics, minus ``enabled``."""
        last = self._last_backfill_at
        return {
            "connected": self._connected,
            "endpoint": f"{NAAD_STREAM_HOST}:{NAAD_STREAM_PORT}",
            "live_documents": len(self._live_docs),
            "last_backfill": last.isoformat() if last is not None else None,
            # Heartbeat-driven recovery from the NAAD short-term repository
            # (issue #164): the count says whether it has ever fired.
            "repository": NAAD_REPOSITORY_URL,
            "repository_recovered": self._repository_recovered,
        }

    def _on_stream_connection_change(self, connected: bool) -> None:
        """Publish a socket connect/disconnect to entity listeners.

        Only notifies listeners — it must not touch ``last_update_success``: a
        dropped socket is not a failed data refresh (issue #16), and the entry
        stays available on backfills while the client reconnects.
        """
        self._connected = connected
        self._host.notify_connection(connected)

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def _build_kwargs(
        self, config: Mapping[str, Any], options: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Region/language kwargs for build_alerts_from_cap_docs from resolved config."""
        gps_lat, gps_lon = self._provider._parse_gps(config)
        return {
            "province": config.get(CONF_PROVINCE, ""),
            "gps_lat": gps_lat,
            "gps_lon": gps_lon,
            "preferred_lang": options.get(CONF_LANGUAGE, "en-CA"),
        }

    def _admit(
        self, docs: list[CAPDoc], build_kwargs: Mapping[str, Any]
    ) -> list[CAPDoc]:
        """Screen streamed docs down to the ones worth holding in the live set.

        The socket carries every alert in Canada, so admitting everything would
        size the live set — and the rebuild it feeds on every stream event — by
        national volume rather than by the configured region. A doc is kept when
        it matches the region, or when it references something already tracked:
        the latter so an update or cancellation still supersedes an alert we hold
        even if its revised geometry no longer covers the user.

        Admission is deliberately wider than the rebuild that follows:
        ``doc_matches_region`` matches on *any* ``<info>`` block, including one
        whose area group has already ended. That is the point — an ECCC document
        segments into a block per area group, and the block that ends a tracked
        alert is often the only one still covering the user. Screening it out
        here would leave the entity live until its stale ``expires`` (issue #45).
        Which block actually speaks for this region, and whether it is terminal,
        is decided later by ``build_alerts_from_cap_docs``.

        Test/exercise traffic is rejected up front rather than left to
        ``doc_matches_region``, since the references escape bypasses that check —
        and a heartbeat's ``<references>`` lists recent alert OIDs, so a heartbeat
        that ever escaped classification would otherwise be admitted once a minute.
        """
        kept: list[CAPDoc] = []
        for doc in docs:
            if not is_actual(doc):
                continue
            if doc_matches_region(doc, **build_kwargs) or any(
                ref_id in self._live_docs for _, ref_id, _ in doc.references
            ):
                kept.append(doc)
        return kept

    def _note_seen(self, docs: list[CAPDoc]) -> None:
        """Record that these documents have been received, whatever admission says.

        A document with no ``sent`` is stamped with the current time so it still
        ages out; an identifier that never aged out would stay "seen" for the
        life of the entry.
        """
        now_text = datetime.now(timezone.utc).isoformat()
        for doc in docs:
            if doc.identifier:
                self._seen[doc.identifier] = doc.sent or now_text

    def _merge_docs(self, docs: list[CAPDoc]) -> None:
        """Upsert docs into the live set by CAP identifier and prune stale ones."""
        self._note_seen(docs)
        for doc in docs:
            if doc.identifier:
                self._live_docs[doc.identifier] = doc
        # The NAAD feeds carry a rolling 48 h window; drop anything older so the
        # live set stays bounded and superseded/expired docs age out. The seen
        # set follows the same clock, since the repository it guards fetches
        # from holds the same window.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        stale = [
            identifier
            for identifier, doc in self._live_docs.items()
            if _sent_before(doc.sent, cutoff)
        ]
        for identifier in stale:
            del self._live_docs[identifier]
        forgotten = [
            identifier
            for identifier, sent in self._seen.items()
            if _sent_before(sent, cutoff)
        ]
        for identifier in forgotten:
            del self._seen[identifier]

    async def async_backfill(
        self, config: Mapping[str, Any], options: Mapping[str, Any]
    ) -> dict[str, CAPAlert]:
        """Seed/re-sync the live set from the GeoRSS feed and rebuild the active set.

        Runs under the ingest lock so it cannot interleave with a stream push —
        the shared pipeline runs inside it too, so a streamed document cannot
        land between the merge and the apply. Raises ProviderError on fetch
        failure, which the host turns into unavailability (issue #16).
        """
        provider = self._provider
        async with self._ingest_lock:
            # Stamped before the fetch, and whether or not it succeeds: what the
            # reconnect throttle has to bound is the ~7 MB transfer, which a
            # failing feed costs just the same.
            self._last_backfill_at = datetime.now(timezone.utc)
            try:
                async with asyncio.timeout(self._host.timeout):
                    docs = await provider.async_fetch_docs(
                        async_get_clientsession(self._host.hass),
                        config,
                        options,
                        cap_content_cache=self._host.cap_content_cache,
                        user_agent=self._host.user_agent,
                    )
            except TimeoutError as err:
                raise ProviderError(
                    f"{self._provider.name}: timeout after {self._host.timeout}s"
                ) from err
            except aiohttp.ClientError as err:
                raise ProviderError(f"{self._provider.name}: {err}") from err

            self._merge_docs(docs)
            alerts = build_alerts_from_cap_docs(
                list(self._live_docs.values()), **self._build_kwargs(config, options)
            )
            data = await self._host.async_apply(alerts, fetched=True)
        self.last_update_success_time = datetime.now(timezone.utc)
        return data

    async def async_ingest_docs(self, docs: list[CAPDoc]) -> None:
        """Merge streamed docs into the live set, rebuild, and push to entities.

        Called by the stream client for each alert doc (``docs=[doc]``) and for
        each heartbeat (``docs=[]``) — the heartbeat rebuild ages out alerts that
        have since expired, with no network I/O.
        """
        try:
            config, options = self._host.resolve_scope()
        except ScopeUnresolvedError:
            # Region unresolvable right now (e.g. tracker has no location) — drop
            # this push; the next backfill re-seeds from the authoritative feed.
            return
        build_kwargs = self._build_kwargs(config, options)
        async with self._ingest_lock:
            self._merge_docs(self._admit(docs, build_kwargs))
            alerts = build_alerts_from_cap_docs(
                list(self._live_docs.values()), **build_kwargs
            )
            data = await self._host.async_apply(alerts, fetched=False)
        self._host.push(data)

    async def _on_backfill_needed(self) -> None:
        """GeoRSS backfill requested by the stream client on reconnect.

        Throttled against the last backfill from either source. The client's
        backoff only grows for connections that delivered nothing, so an endpoint
        that sends a heartbeat and then drops reconnects at the heartbeat (or
        watchdog) cadence with the backoff pinned at its floor — and each
        reconnect would otherwise pay a full ~7 MB feed fetch, making a flapping
        socket more expensive than the polling it replaced. Skipping is safe: the
        periodic resync still runs, and a backfill within the last few minutes has
        already recovered essentially everything this one would.

        A transient backfill failure here does not flip availability (issue #16) —
        the periodic ``_async_update_data`` backfill is the authoritative signal.
        It is still worth a warning, once per failure streak, since a persistently
        failing reconnect backfill means alerts missed while disconnected are not
        being recovered.
        """
        last = self._last_backfill_at
        if last is not None and datetime.now(timezone.utc) - last < timedelta(
            seconds=NAAD_STREAM_BACKFILL_MIN_INTERVAL_S
        ):
            _LOGGER.debug(
                "ECCC: skipping reconnect backfill; one ran %.0fs ago (floor %ds)",
                (datetime.now(timezone.utc) - last).total_seconds(),
                NAAD_STREAM_BACKFILL_MIN_INTERVAL_S,
            )
            return
        try:
            config, options = self._host.resolve_scope()
            data = await self.async_backfill(config, options)
        except ProviderError as err:
            if not self._stream_backfill_warned:
                _LOGGER.warning(
                    "ECCC: stream-triggered backfill failed: %s; alerts issued "
                    "while disconnected may be missing until the next resync",
                    err,
                )
                self._stream_backfill_warned = True
            return
        self._stream_backfill_warned = False
        self._host.push(data)

    async def _on_stream_alert_doc(self, doc_str: str) -> None:
        loop = asyncio.get_running_loop()
        doc = await loop.run_in_executor(None, parse_cap_alert, doc_str)
        if doc is None or not doc.identifier:
            return
        # Seen is recorded before admission on purpose: a national alert outside
        # the region is still one the heartbeat will list, and the only way to
        # know not to fetch it is to remember it arrived.
        self._note_seen([doc])
        await self.async_ingest_docs([doc])

    async def _on_stream_heartbeat(self, doc_str: str) -> None:
        """Rebuild on a heartbeat, first recovering anything the stream missed.

        A heartbeat's ``<references>`` lists the last ten alerts NAAD published,
        and the short-term repository serves each by a URL built from that
        reference (issue #164). Any identifier not already seen — on the
        socket, in a backfill, or from an earlier recovery — is fetched and fed
        through the normal ingest, where ``_admit`` screens it by region exactly
        as if it had streamed. That closes the one gap streaming has: an alert
        issued in a reconnect window. The reconnect backfill is throttled to
        the old poll cadence and the GeoRSS index omits live alerts daily, so
        neither was guaranteed to catch it; this path is bounded to ten small
        fetches per heartbeat and gated by nothing.

        Steady state costs nothing: every streamed alert is noted before the
        heartbeat that lists it. The first heartbeat after a (re)connect fetches
        what the window holds that this entry has not seen — up to ten bodies,
        most of them out of region and discarded on admission — which is also
        how an alert the GeoRSS seed omitted gets recovered at startup.

        The fetch runs inline, so the read loop waits on it: at most
        ``timeout`` once per (re)connect, with the kernel buffering the socket.
        """
        loop = asyncio.get_running_loop()
        heartbeat = await loop.run_in_executor(None, parse_cap_alert, doc_str)
        references = self._unseen_references(
            heartbeat.references if heartbeat is not None else []
        )
        recovered = await self._recover_from_repository(references)
        await self.async_ingest_docs(recovered)

    def _unseen_references(
        self, references: Sequence[tuple[str, str, str]]
    ) -> list[tuple[str, str, str]]:
        """The heartbeat references still worth fetching.

        Skips anything seen, anything whose ``sent`` is past the 48 h window the
        repository holds (the live set would prune it on arrival anyway), and
        anything already given up on. Attempt counts for identifiers no longer
        in the window are dropped here, which keeps that dict bounded by the
        window's size.
        """
        in_window = {identifier for _, identifier, _ in references}
        self._repository_attempts = {
            identifier: count
            for identifier, count in self._repository_attempts.items()
            if identifier in in_window
        }
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        return [
            (sender, identifier, sent)
            for sender, identifier, sent in references
            if identifier
            and identifier not in self._seen
            and not _sent_before(sent, cutoff)
        ]

    async def _recover_from_repository(
        self, references: Sequence[tuple[str, str, str]]
    ) -> list[CAPDoc]:
        """Fetch ``references`` from the NAAD repository, bookkeeping the misses.

        A fetched document is marked seen and counted. A miss is counted against
        ``NAAD_REPOSITORY_FETCH_ATTEMPTS``; at the bound the identifier is
        marked seen and warned about once, so a body the repository will never
        serve stops costing a fetch and a log line per heartbeat. A transport
        failure of the whole batch (timeout, client error) is debug-logged and
        retried on the next heartbeat without touching the counts.
        """
        provider = self._provider
        if not references:
            return []
        _LOGGER.info(
            "ECCC: fetching %d alert(s) the NAAD heartbeat lists but the stream "
            "did not deliver: %s",
            len(references),
            ", ".join(identifier for _, identifier, _ in references),
        )
        try:
            async with asyncio.timeout(self._host.timeout):
                results = await provider.async_fetch_docs_by_reference(
                    async_get_clientsession(self._host.hass),
                    references,
                    cap_content_cache=self._host.cap_content_cache,
                    user_agent=self._host.user_agent,
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug(
                "ECCC: repository fetch failed (%s); retrying on the next heartbeat",
                err,
            )
            return []

        recovered: list[CAPDoc] = []
        for _sender, identifier, sent in references:
            doc = results.get(identifier)
            if doc is not None:
                self._seen[identifier] = doc.sent or sent
                self._repository_attempts.pop(identifier, None)
                self._repository_recovered += 1
                recovered.append(doc)
                continue
            attempts = self._repository_attempts.get(identifier, 0) + 1
            if attempts < NAAD_REPOSITORY_FETCH_ATTEMPTS:
                self._repository_attempts[identifier] = attempts
                continue
            self._repository_attempts.pop(identifier, None)
            self._seen[identifier] = sent
            _LOGGER.warning(
                "ECCC: giving up on %s after %d failed NAAD repository fetches; "
                "if it concerned this region it will arrive on the next resync",
                identifier,
                attempts,
            )
        return recovered

    async def async_start(self) -> None:
        """Start the NAAD stream background task (no-op unless streaming). Idempotent."""
        if self._stream_task is not None:
            return
        # Build the TLS context off the event loop: it reads the CA bundle from
        # disk, and HA flags that as a blocking call. ``client_context`` is HA's
        # certifi-backed client context and is itself cached, so entries after
        # the first pay nothing. Note it advertises no ALPN protocol — the NAAD
        # socket carries raw CAP, not HTTP, so ``get_default_context`` (which
        # pins ALPN to http/1.1) would be wrong here.
        ssl_context = await self._host.hass.async_add_executor_job(client_context)
        self._stream_client = NAADStreamClient(
            NAAD_STREAM_HOST,
            NAAD_STREAM_PORT,
            on_alert_doc=self._on_stream_alert_doc,
            on_heartbeat=self._on_stream_heartbeat,
            on_backfill_needed=self._on_backfill_needed,
            on_connection_change=self._on_stream_connection_change,
            ssl_context=ssl_context,
            logger=_LOGGER,
        )
        self._stream_task = self._host.hass.async_create_background_task(
            self._stream_client.run(),
            name=f"{DOMAIN}_naad_stream_{self._host.entry_id}",
        )

    async def async_stop(self) -> None:
        """Stop the NAAD stream task and client. Idempotent; no task leak."""
        client = self._stream_client
        task = self._stream_task
        self._stream_client = None
        self._stream_task = None
        if client is not None:
            client.stop()
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as err:  # noqa: BLE001 — teardown best-effort
                _LOGGER.debug("ECCC: stream task raised on teardown: %s", err)
        # run()'s finally normally publishes the disconnect, but a client that
        # never started (or a task cancelled before it ran) leaves the flag set
        # from a previous connection. Clear it directly; entities are being torn
        # down anyway, so no notification is needed.
        self._connected = False
