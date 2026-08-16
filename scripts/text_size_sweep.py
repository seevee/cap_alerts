#!/usr/bin/env python3
"""Measure long-form text and attribute-payload sizes across every provider.

Sizes the bound. ``payload.PAYLOAD_BUDGET`` is a number someone has to pick, and
picking it from intuition is how the reference implementation ended up capping
``description`` at 4 KB while leaving ``description_alt`` unbounded — a hole no
test caught because no test knew what real feeds send.

Four questions, in the order they matter:

1. **How long is long-form text, per field?** Descriptions, instructions, and
   their localized twins, per provider, in UTF-8 bytes.
2. **How long is it per alert, summed across the four fields?** The figure an
   aggregate budget would be set from, and the one the first sweep omitted.
3. **What does an alert actually serialize to?** ``to_attributes()`` against the
   recorder's 16,384-byte ceiling. This is the question that matters, because a
   per-field cap that truncates text on an alert which would have fit is pure
   loss.
4. **What does the budget leave?** The same payload through
   ``payload.fit_to_budget``, measured the way the recorder measures it. Nothing
   should come out over the budget; how much text it cost to get there is the
   price of the fix (issue #150).

Usage (needs the test venv, which has aiohttp + homeassistant)::

    .venv/bin/python scripts/text_size_sweep.py
    .venv/bin/python scripts/text_size_sweep.py --providers nws,eccc
    .venv/bin/python scripts/text_size_sweep.py --wmo-limit 20 --concurrency 4
    .venv/bin/python scripts/text_size_sweep.py --json > sweep.json

A full run is 170-odd scopes and several thousand HTTP requests, most of them
WMO CAP bodies. Budget 5-10 minutes and expect some 404s; the mirror is missing
bodies for sources the registry still lists.

Long-form text is measured as the provider parsed it. Normalization no longer
touches those fields, so this is also what the entity would publish before the
budget gets a say.

Payloads are a floor, not a total: ``geometry_ref`` and ``bbox`` are written by
the coordinator and ``incident_platform_version`` by the entity, so a real state
object carries roughly 150 bytes this script never sees, plus the
``friendly_name`` Home Assistant appends afterwards — which is what
``payload.PAYLOAD_RESERVE`` exists to cover.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.cap_alerts.const import (  # noqa: E402
    CONF_ALERT_LEVEL,
    ECCC_PROVINCES,
    METEOALARM_COUNTRIES,
    USER_AGENT,
)
from custom_components.cap_alerts.conventions import (  # noqa: E402
    StageContext,
    conventions_for,
)
from custom_components.cap_alerts.model import CAPAlert  # noqa: E402
from custom_components.cap_alerts.normalize import normalize_alerts  # noqa: E402
from custom_components.cap_alerts.payload import (  # noqa: E402
    PAYLOAD_BUDGET,
    fit_to_budget,
    measure,
)
from custom_components.cap_alerts.providers import get_provider  # noqa: E402
from custom_components.cap_alerts.providers.cap_content_cache import (  # noqa: E402
    CAPContentCache,
)
from custom_components.cap_alerts.providers.nws import (  # noqa: E402
    NWS_API_BASE,
    _parse_feature,
)
from custom_components.cap_alerts.providers.wmo import fetch_wmo_sources  # noqa: E402

# The recorder's ceiling (homeassistant/components/recorder/db_schema.py).
CEILING = 16384

# The four fields an aggregate long-form budget would cover.
LONG_FORM = ("description", "instruction", "description_alt", "instruction_alt")

# Size histogram edges, in bytes. Chosen to straddle the 4,096 cap so the
# shape either side of it is visible rather than pooled.
BINS = (256, 512, 1024, 1536, 2048, 2560, 3072, 3584, 4096, 4608, 6144, 8192)

ALL_PROVIDERS = ("nws", "eccc", "meteoalarm", "wmo", "gdacs")


def _blen(text: str | None) -> int:
    """UTF-8 byte length, matching ``_soft_cap``'s own measurement."""
    return len(text.encode("utf-8")) if text else 0


def _payload_bytes(attrs: dict[str, Any]) -> int:
    """Serialized attribute size, the way the recorder would encode it."""
    return len(json.dumps(attrs, separators=(",", ":"), default=str))


# ---------------------------------------------------------------------------
# Scope enumeration
# ---------------------------------------------------------------------------


async def _wmo_scopes(
    session: aiohttp.ClientSession, limit: int | None
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Every mirror-reachable SWIC source, from the live registry."""
    sources = await fetch_wmo_sources(session, user_agent=USER_AGENT.format("sweep"))
    if limit is not None:
        sources = sources[:limit]
    return [
        (f"wmo:{sid}", {"provider": "wmo", "source_id": sid}, {})
        for sid, _label in sources
    ]


async def _scopes_for(
    provider: str, session: aiohttp.ClientSession, args: argparse.Namespace
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """``[(label, config, options), ...]`` covering a provider's whole surface."""
    if provider == "eccc":
        return [
            (f"eccc:{p}", {"provider": "eccc", "province": p}, {})
            for p in sorted(ECCC_PROVINCES)
        ]
    if provider == "meteoalarm":
        return [
            (f"meteoalarm:{c}", {"provider": "meteoalarm", "country": c}, {})
            for c in sorted(METEOALARM_COUNTRIES)
        ]
    if provider == "wmo":
        return await _wmo_scopes(session, args.wmo_limit)
    if provider == "gdacs":
        # One global scope, and the lowest floor so nothing is filtered out.
        return [
            ("gdacs:global", {"provider": "gdacs"}, {CONF_ALERT_LEVEL: "Green"}),
        ]
    if provider == "nws":
        return [("nws:national", {"provider": "nws"}, {})]
    raise ValueError(f"unknown provider {provider}")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def _fetch_nws_national(session: aiohttp.ClientSession) -> list[CAPAlert]:
    """The national active set.

    The provider has no national scope — ``_build_url`` returns ``""`` without a
    zone, GPS or tracker — so this drives ``_parse_feature`` over the unscoped
    endpoint directly, then runs the ``merge`` convention stages the provider
    would have run. Note those stages deliberately pass VTEC-bearing alerts
    through untouched, so a tropical warning still arrives as one record per
    zone, each with its own description.
    """
    headers = {
        "User-Agent": USER_AGENT.format("sweep"),
        "Accept": "application/geo+json",
    }
    async with session.get(NWS_API_BASE, headers=headers) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    alerts = [_parse_feature(f) for f in data.get("features", [])]
    ctx = StageContext(now=datetime.now(UTC))
    for run in conventions_for("nws").stages_at("merge"):
        alerts = run(alerts, ctx)
    return alerts


async def _fetch_scope(
    session: aiohttp.ClientSession,
    label: str,
    config: dict[str, Any],
    options: dict[str, Any],
    sem: asyncio.Semaphore,
    verbose: bool,
) -> tuple[str, list[CAPAlert], str | None]:
    """Fetch one scope, returning its alerts or the error that stopped it."""
    async with sem:
        try:
            if config["provider"] == "nws" and "zone_id" not in config:
                alerts = await _fetch_nws_national(session)
            else:
                alerts = await get_provider(config["provider"]).async_fetch(
                    session,
                    config,
                    options,
                    cap_content_cache=CAPContentCache(),
                    user_agent=USER_AGENT.format("sweep"),
                )
        except Exception as err:  # noqa: BLE001 - a dead scope is data, not a crash
            if verbose:
                print(f"  {label}: {type(err).__name__}: {err}", file=sys.stderr)
            return label, [], f"{type(err).__name__}: {err}"
    if verbose:
        print(f"  {label}: {len(alerts)}", file=sys.stderr)
    return label, alerts, None


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _measure(provider: str, alerts: list[CAPAlert]) -> list[dict[str, Any]]:
    """One row per alert: field sizes, their sum, and both payload figures.

    Normalization runs first so the row carries the derived attributes an entity
    would have. ``payload`` is what the alert serializes to untouched;
    ``recorded`` is the same payload through the budget, measured the way the
    recorder measures it (unrecorded attributes excluded).
    """
    rows: list[dict[str, Any]] = []
    for alert in normalize_alerts(alerts, "sweep"):
        attrs = alert.to_attributes()
        fitted = fit_to_budget(attrs)
        payload = _payload_bytes(attrs)
        sizes = {f: _blen(getattr(alert, f)) for f in LONG_FORM}
        rows.append(
            {
                "provider": provider,
                "id": alert.id,
                "event": alert.event,
                **sizes,
                "long_form_total": sum(sizes.values()),
                "payload": payload,
                "recorded": measure(fitted) or 0,
                # ``fit_to_budget`` returns the input untouched when it fits.
                "lost": payload - _payload_bytes(fitted) if fitted is not attrs else 0,
                "keys": len(attrs),
            }
        )
    return rows


def _pct(values: list[int], q: float) -> int:
    """Nearest-rank percentile. Plain ``max``/``min`` at the extremes."""
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _field_table(rows: list[dict[str, Any]], field: str) -> None:
    """Per-provider distribution of one field, non-empty values only."""
    by_provider: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        if row[field]:
            by_provider[row["provider"]].append(row[field])
    if not by_provider:
        print(f"\n{field}: no non-empty values")
        return
    print(f"\n{field}")
    print(
        f"  {'provider':12s} {'n':>6s} {'>4096':>6s} {'p50':>7s} {'p90':>7s} {'max':>7s}"
    )
    everything: list[int] = []
    for provider in sorted(by_provider):
        values = by_provider[provider]
        everything.extend(values)
        over = sum(1 for v in values if v > 4096)
        print(
            f"  {provider:12s} {len(values):6d} {over:6d} "
            f"{_pct(values, 0.50):7d} {_pct(values, 0.90):7d} {max(values):7d}"
        )
    over_all = sum(1 for v in everything if v > 4096)
    print(
        f"  {'ALL':12s} {len(everything):6d} {over_all:6d} "
        f"{_pct(everything, 0.50):7d} {_pct(everything, 0.90):7d} {max(everything):7d}"
    )


def _histogram(values: list[int], label: str) -> None:
    """Bucket counts, so the shape either side of a candidate cap is visible."""
    if not values:
        return
    counts = [0] * (len(BINS) + 1)
    for value in values:
        for i, edge in enumerate(BINS):
            if value < edge:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    print(f"\n{label} ({len(values)} values)")
    low = 0
    widest = max(counts) or 1
    for i, count in enumerate(counts):
        high = f"{BINS[i]:,}" if i < len(BINS) else "+inf"
        bar = "#" * round(40 * count / widest)
        print(f"  {low:>7,}-{high:>7} {count:6d} {bar}")
        low = BINS[i] if i < len(BINS) else low


def _report(rows: list[dict[str, Any]], errors: dict[str, str]) -> None:
    """The whole point: the three questions in the module docstring."""
    print(f"\n{'=' * 72}\n{len(rows)} alerts measured")
    if errors:
        print(f"{len(errors)} scopes failed (listed at the end)")

    for field in LONG_FORM:
        _field_table(rows, field)

    totals = [r["long_form_total"] for r in rows if r["long_form_total"]]
    _histogram(totals, "long-form total per alert (all four fields)")
    if totals:
        print(
            f"  p50={_pct(totals, 0.50):,}  p90={_pct(totals, 0.90):,}  "
            f"p99={_pct(totals, 0.99):,}  max={max(totals):,}  "
            f"mean={statistics.mean(totals):,.0f}"
        )

    payloads = [r["payload"] for r in rows]
    _histogram(payloads, f"serialized payload per alert (ceiling {CEILING:,})")
    if payloads:
        over = [r for r in rows if r["payload"] > CEILING]
        print(
            f"  p50={_pct(payloads, 0.50):,}  p90={_pct(payloads, 0.90):,}  "
            f"p99={_pct(payloads, 0.99):,}  max={max(payloads):,}"
        )
        print(f"  over the ceiling uncapped: {len(over)} of {len(payloads)}")
        for row in sorted(rows, key=lambda r: -r["payload"])[:5]:
            flag = "OVER" if row["payload"] > CEILING else "ok"
            print(
                f"    {row['payload']:7,}  {flag:4s}  {row['provider']:10s} "
                f"long_form={row['long_form_total']:6,}  {str(row['event'])[:38]}"
            )

    recorded = [r["recorded"] for r in rows]
    if recorded:
        trimmed = [r for r in rows if r["lost"]]
        still_over = [r for r in rows if r["recorded"] > PAYLOAD_BUDGET]
        print(f"\nafter the budget ({PAYLOAD_BUDGET:,}), as the recorder measures it")
        print(
            f"  p50={_pct(recorded, 0.50):,}  p90={_pct(recorded, 0.90):,}  "
            f"p99={_pct(recorded, 0.99):,}  max={max(recorded):,}"
        )
        print(f"  trimmed: {len(trimmed)} of {len(recorded)}")
        print(f"  still over the budget: {len(still_over)}")
        for row in sorted(trimmed, key=lambda r: -r["lost"])[:5]:
            print(
                f"    -{row['lost']:6,}  {row['payload']:7,} uncapped  "
                f"{row['provider']:10s} {str(row['event'])[:38]}"
            )

    if errors:
        print(f"\n{len(errors)} failed scopes")
        for label, err in sorted(errors.items())[:20]:
            print(f"  {label}: {err}")
        if len(errors) > 20:
            print(f"  ... {len(errors) - 20} more")


# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in providers if p not in ALL_PROVIDERS]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    sem = asyncio.Semaphore(args.concurrency)
    timeout = aiohttp.ClientTimeout(total=args.deadline)
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for provider in providers:
            scopes = await _scopes_for(provider, session, args)
            print(f"{provider}: {len(scopes)} scopes", file=sys.stderr)
            results = await asyncio.gather(
                *(
                    _fetch_scope(session, label, config, options, sem, args.verbose)
                    for label, config, options in scopes
                )
            )
            for label, alerts, err in results:
                if err:
                    errors[label] = err
                if alerts:
                    rows.extend(_measure(provider, alerts))

    elapsed = time.monotonic() - started
    if args.json:
        json.dump(
            {"elapsed": elapsed, "ceiling": CEILING, "rows": rows, "errors": errors},
            sys.stdout,
            indent=2,
        )
        return 0

    _report(rows, errors)
    print(f"\nswept in {elapsed:.0f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--providers",
        default=",".join(ALL_PROVIDERS),
        help=f"comma-separated subset of {','.join(ALL_PROVIDERS)}",
    )
    parser.add_argument(
        "--wmo-limit",
        type=int,
        help="cap the number of WMO sources swept (the slow half of a full run)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=8, help="concurrent scope fetches"
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=900.0,
        help="HTTP session deadline in seconds (default 900)",
    )
    parser.add_argument(
        "--json", action="store_true", help="dump per-alert rows instead of the report"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="per-scope progress on stderr"
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
