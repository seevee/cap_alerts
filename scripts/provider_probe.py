#!/usr/bin/env python3
"""Run a provider's fetch outside Home Assistant and report what came back.

Answers feed-shape questions ("does this source publish polygons?", "which
``<info>`` language does this option select?", "would GPS mode work here?")
against the *real* provider code path, in seconds, without a container
restart or a config-entry reload. The alternative — reconfiguring a live
entry and waiting for a coordinator cycle — costs minutes per question and
conflates provider behaviour with HA setup/timeout behaviour.

Usage (needs the test venv, which has aiohttp + homeassistant):

    .venv/bin/python scripts/provider_probe.py wmo --source cn-cma-xx --language zh
    .venv/bin/python scripts/provider_probe.py wmo --source cn-cma-xx --gps 39.9042,116.4074
    .venv/bin/python scripts/provider_probe.py meteoalarm --country CH --gps 46.2044,6.1432
    .venv/bin/python scripts/provider_probe.py eccc --province BC --language fr-CA
    .venv/bin/python scripts/provider_probe.py nws --zone MOC217

Arbitrary keys for providers this script has no flag for:

    .venv/bin/python scripts/provider_probe.py nws -c zone_id=MIZ020 -o timeout=60

``--json`` dumps the full ``to_attributes()`` payload per alert; the default
is a summary plus the per-alert one-liners. ``--normalize`` additionally runs
``normalize_alerts()`` so severity_normalized/phase are visible — the raw
provider output is what the GPS filter and the language selector see, the
normalized output is what entities get.

The geocode-prefix option (issue #73) is applied by the *coordinator*, not the
provider, so it is reproduced here in the same position — after normalization —
using the same function HA calls. This is how you size an entry before creating
it, and how you find a prefix worth typing:

    .venv/bin/python scripts/provider_probe.py wmo --source cn-cma-xx --codes
    .venv/bin/python scripts/provider_probe.py wmo --source cn-cma-xx --geocode-prefix 13

``--codes`` buckets the area codes the source actually publishes, at the widths
a user would plausibly type — the substitute for the region picker this filter
deliberately does not have (enumerating areas would cost a full CAP-body sweep).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.cap_alerts.const import USER_AGENT  # noqa: E402
from custom_components.cap_alerts.coordinator import (  # noqa: E402
    filter_by_geocode_prefixes,
)
from custom_components.cap_alerts.model import CAPAlert  # noqa: E402
from custom_components.cap_alerts.normalize import normalize_alerts  # noqa: E402
from custom_components.cap_alerts.providers import get_provider  # noqa: E402
from custom_components.cap_alerts.providers.cap_content_cache import (  # noqa: E402
    CAPContentCache,
)

# Flags that map onto config keys, so the common cases don't need `-c k=v`.
CONFIG_FLAGS = {
    "source": "source_id",
    "country": "country",
    "province": "province",
    "zone": "zone_id",
    "gps": "gps_loc",
    "regions": "regions",
}
OPTION_FLAGS = {"language": "language", "timeout": "timeout"}

# Option keys HA stores as ``list[str]``, so ``-o k=a,b`` round-trips to the
# same shape the real options flow writes.
LIST_KEYS = {"regions", "geocode_prefixes"}


def _kv(pairs: list[str]) -> dict[str, Any]:
    """Parse repeated ``k=v`` arguments, coercing ints and comma-lists."""
    out: dict[str, Any] = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        if key in LIST_KEYS:
            out[key] = [r for r in (p.strip() for p in raw.split(",")) if r]
        elif raw.isdigit():
            out[key] = int(raw)
        else:
            out[key] = raw
    return out


def _describe(alert: CAPAlert) -> str:
    """One line per alert: the fields that distinguish feed shapes."""
    geom = alert.geometry.get("type") if alert.geometry else "-"
    schemes = ",".join(sorted(alert.geocodes)) if alert.geocodes else "-"
    return (
        f"  {str(alert.severity or '-'):9s} {str(alert.event or '-')[:34]:34s} "
        f"phase={str(alert.phase or '-'):9s} "
        f"lang={str(alert.language or '-'):7s} alt={str(alert.language_alt or '-'):7s} "
        f"geom={str(geom):12s} codes={schemes[:28]:28s} {str(alert.area_desc or '')[:30]}"
    )


def _summarize(alerts: list[CAPAlert], elapsed: float) -> None:
    """Print the aggregate view: counts, geometry coverage, language spread.

    ``phase`` is only populated after ``--normalize``; it is the field that
    explains a probe/entity count mismatch, since the coordinator's store
    publishes only non-terminal alerts while this script reports everything
    the provider returned.
    """
    print(f"\n{len(alerts)} alerts in {elapsed:.1f}s")
    if not alerts:
        return
    with_geom = sum(1 for a in alerts if a.geometry)
    print(f"  geometry:      {with_geom}/{len(alerts)} carry polygons", end="")
    if with_geom == 0:
        print("  <- GPS/tracker mode CANNOT work for this source")
    elif with_geom < len(alerts):
        print("  <- partial; GPS mode silently drops the polygonless ones")
    else:
        print()
    for label, values in (
        ("language", Counter(a.language or "-" for a in alerts)),
        ("language_alt", Counter(a.language_alt or "-" for a in alerts)),
        ("severity", Counter(a.severity or "-" for a in alerts)),
        ("msg_type", Counter(a.msg_type or "-" for a in alerts)),
        ("phase", Counter(a.phase or "-" for a in alerts)),
    ):
        print(f"  {label + ':':14s} {dict(values.most_common(8))}")
    schemes: Counter[str] = Counter()
    for alert in alerts:
        # .keys() matters: Counter.update on a {scheme: (codes…)} mapping reads
        # the tuples as counts and concatenates them into one unreadable blob.
        schemes.update(alert.geocodes.keys())
    print(f"  {'geocodes:':14s} {dict(schemes.most_common(8))}", end="")
    with_codes = sum(1 for a in alerts if a.geocodes)
    if with_codes == 0:
        print("  <- geocode-prefix filter CANNOT work (fails loud)")
    elif with_codes < len(alerts):
        print(f"  <- partial ({with_codes}/{len(alerts)}); the rest can never match")
    else:
        print()


def _code_buckets(alerts: list[CAPAlert], widths: tuple[int, ...] = (2, 4, 6)) -> None:
    """Bucket published area codes by prefix width, widest scope first.

    Stands in for the region picker the filter has no cheap way to build:
    hierarchical codes mean each width is an administrative level, so this
    shows what is worth typing and how much each choice would keep. Counts are
    alerts, not codes — an alert matches a prefix once however many codes it
    carries.
    """
    codes = [(a, c) for a in alerts for codes in a.geocodes.values() for c in codes]
    if not codes:
        print("\nno area codes published — nothing to bucket")
        return
    lengths = Counter(len(c) for _a, c in codes)
    print(f"\narea codes: {len(codes)} values, lengths {dict(lengths.most_common(6))}")
    for width in widths:
        # Distinct alerts per prefix, not distinct codes — one alert carrying
        # several codes under the same prefix is one entity, not several.
        buckets: dict[str, set[str]] = {}
        for alert, code in codes:
            if len(code) >= width:
                buckets.setdefault(code[:width], set()).add(alert.id)
        if not buckets:
            continue
        ranked = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        shown = ", ".join(f"{p}={len(ids)}" for p, ids in ranked[:12])
        print(f"  {width}-char prefixes ({len(buckets)} distinct): {shown}")


async def _run(args: argparse.Namespace) -> int:
    config: dict[str, Any] = {"provider": args.provider}
    config.update(_kv(args.config))
    options: dict[str, Any] = _kv(args.option)
    for flag, key in CONFIG_FLAGS.items():
        value = getattr(args, flag, None)
        if value:
            config[key] = (
                [r.strip() for r in value.split(",")] if key == "regions" else value
            )
    for flag, key in OPTION_FLAGS.items():
        value = getattr(args, flag, None)
        if value is not None:
            options[key] = value

    print(f"provider={args.provider} config={config} options={options}")

    provider = get_provider(args.provider)
    timeout = aiohttp.ClientTimeout(total=args.deadline)
    started = time.monotonic()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            alerts = await provider.async_fetch(
                session,
                config,
                options,
                cap_content_cache=CAPContentCache(),
                user_agent=USER_AGENT.format("probe"),
            )
        except Exception as err:  # noqa: BLE001 - the failure IS the result
            elapsed = time.monotonic() - started
            print(f"\n{type(err).__name__} after {elapsed:.1f}s: {err}")
            return 1
    elapsed = time.monotonic() - started

    if args.normalize or args.active:
        alerts = normalize_alerts(alerts)

    # What the store publishes. Applied before the geocode filter rather than
    # after (as the coordinator does) purely for readable output — both are
    # pure filters, so the surviving set is identical either way.
    if args.active:
        before = len(alerts)
        alerts = [a for a in alerts if a.phase not in ("cancel", "expired")]
        print(f"\nactive (non-terminal) alerts: {before} -> {len(alerts)}")

    # The geocode filter is a coordinator step, not a provider one — applied
    # here in the same position (after normalization) via the same function.
    prefixes = args.geocode_prefix or options.get("geocode_prefixes") or []
    if isinstance(prefixes, str):
        prefixes = [p.strip() for p in prefixes.split(",") if p.strip()]
    if prefixes:
        before = len(alerts)
        try:
            alerts = filter_by_geocode_prefixes(alerts, prefixes)
        except Exception as err:  # noqa: BLE001 - the failure IS the result
            print(f"\ngeocode filter {type(err).__name__}: {err}")
            return 1
        pct = (100.0 * len(alerts) / before) if before else 0.0
        print(
            f"\ngeocode prefixes {','.join(prefixes)}: "
            f"{before} -> {len(alerts)} alerts ({pct:.0f}% kept)"
        )
        if not alerts and before:
            print("  no match — HA would log a one-shot WARNING and stay available")

    if args.codes:
        _code_buckets(alerts)

    if args.json:
        print(json.dumps([a.to_attributes() for a in alerts], indent=2, default=str))
        _summarize(alerts, elapsed)
        return 0

    _summarize(alerts, elapsed)
    if alerts:
        print()
        for alert in alerts[: args.limit]:
            print(_describe(alert))
        if len(alerts) > args.limit:
            print(f"  ... {len(alerts) - args.limit} more (raise --limit)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("provider", choices=["nws", "eccc", "meteoalarm", "wmo"])
    parser.add_argument("--source", help="WMO source_id, e.g. cn-cma-xx")
    parser.add_argument("--country", help="MeteoAlarm ISO-2 country, e.g. CH")
    parser.add_argument("--province", help="ECCC province, e.g. BC")
    parser.add_argument("--zone", help="NWS zone id(s), comma-separated")
    parser.add_argument("--regions", help="MeteoAlarm region codes, comma-separated")
    parser.add_argument("--gps", help='"lat,lon" — enables the GPS polygon filter')
    parser.add_argument("--language", help="language option (provider-specific)")
    parser.add_argument("--timeout", type=int, help="provider timeout option")
    parser.add_argument(
        "-c",
        "--config",
        action="append",
        default=[],
        metavar="K=V",
        help="extra config key (repeatable)",
    )
    parser.add_argument(
        "-o",
        "--option",
        action="append",
        default=[],
        metavar="K=V",
        help="extra option key (repeatable)",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=300.0,
        help="HTTP session deadline in seconds (default 300; heavy WMO sources "
        "like cn-cma-xx need minutes on a cold cache)",
    )
    parser.add_argument(
        "--active",
        action="store_true",
        help="drop cancel/expired alerts (implies --normalize), so counts match "
        "the entities HA would actually create",
    )
    parser.add_argument(
        "--geocode-prefix",
        help="comma-separated area-code prefixes; applies the coordinator's "
        "geocode filter post-fetch (same as the options-flow field)",
    )
    parser.add_argument(
        "--codes",
        action="store_true",
        help="bucket published area codes by prefix width, to pick a prefix",
    )
    parser.add_argument("--limit", type=int, default=20, help="alerts to list")
    parser.add_argument("--json", action="store_true", help="dump to_attributes()")
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="also run normalize_alerts() (severity_normalized, phase)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
