#!/usr/bin/env python3
"""Survey the ``<info>`` language sets multilingual feeds actually publish.

Issue #154: the configured language selects the primary ``<info>`` block, but
nothing selected the alternate — each provider took the first leftover block in
document order. Whether that matters depends on a question no fixture answers:
how many documents carry *three or more distinct languages*, and when they do,
is English among them? This script answers it against the live feeds, for the
two providers whose documents can carry more than two languages (WMO SWIC and
MeteoAlarm). ECCC is bilingual by construction and is not swept.

Read-only. Identifies itself as ``HomeAssistant-CAPAlerts/probe``.

Usage (needs the test venv, which has aiohttp + homeassistant)::

    .venv/bin/python scripts/alt_language_sweep.py
    .venv/bin/python scripts/alt_language_sweep.py --providers wmo --wmo-docs 5
    .venv/bin/python scripts/alt_language_sweep.py --json > langs.json

Per document it records the ordered language tuple, then reports:

1. the distribution of *distinct* language counts (duplicate tags, such as
   ``ca-aema-xx``'s one-block-per-area-group, count once);
2. every 3+-language combination seen, and whether English is in it;
3. a simulation over every preference a user could set (each language the
   document carries, plus ``auto``-resolved English): what the pre-#154 rule
   (first leftover block) hands back as the alternate versus the shipped one
   (``cap.alternate_info_index``, imported rather than re-implemented so the
   sweep can't drift from the code), and how often the two disagree.

Budget a few minutes: one RSS fetch per SWIC source plus up to ``--wmo-docs``
CAP bodies each, and one JSON fetch per MeteoAlarm country.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.cap_alerts.const import (  # noqa: E402
    METEOALARM_COUNTRIES,
    METEOALARM_COUNTRY_SLUGS,
    USER_AGENT,
)
from custom_components.cap_alerts.providers.cap import (  # noqa: E402
    _primary_subtag,
    alternate_info_index,
    parse_cap_alert,
)
from custom_components.cap_alerts.providers.cap_content_cache import (  # noqa: E402
    CAPContentCache,
)
from custom_components.cap_alerts.providers.meteoalarm import (  # noqa: E402
    METEOALARM_FEED_URL,
)
from custom_components.cap_alerts.providers.wmo import (  # noqa: E402
    WMO_RSS_URL,
    _parse_rss_links,
    fetch_wmo_sources,
)

UA = USER_AGENT.format("probe")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def _wmo_source_docs(
    session: aiohttp.ClientSession,
    sid: str,
    per_source: int,
    sem: asyncio.Semaphore,
    verbose: bool,
) -> tuple[str, list[tuple[str, ...]], str | None]:
    """``(source_id, [language tuple per doc], error)`` for one SWIC source."""
    cache = CAPContentCache()
    async with sem:
        try:
            async with session.get(
                WMO_RSS_URL.format(source_id=sid), headers={"User-Agent": UA}
            ) as resp:
                if resp.status != 200:
                    return sid, [], f"RSS HTTP {resp.status}"
                rss = await resp.text()
            urls = _parse_rss_links(rss)[:per_source]
            bodies = await asyncio.gather(
                *(cache.get_or_fetch(session, u, user_agent=UA) for u in urls)
            )
        except Exception as err:  # noqa: BLE001 - a dead source is data
            return sid, [], f"{type(err).__name__}: {err}"
    tuples: list[tuple[str, ...]] = []
    for body in bodies:
        if body is None:
            continue
        doc = parse_cap_alert(body)
        if doc is None:
            continue
        tuples.append(tuple(info.language.strip() for info in doc.infos))
    if verbose:
        print(f"  wmo:{sid}: {len(tuples)} docs", file=sys.stderr)
    return sid, tuples, None


async def _meteoalarm_country_docs(
    session: aiohttp.ClientSession,
    country: str,
    sem: asyncio.Semaphore,
    verbose: bool,
) -> tuple[str, list[tuple[str, ...]], str | None]:
    """``(country, [language tuple per warning], error)`` for one country."""
    url = METEOALARM_FEED_URL.format(country=METEOALARM_COUNTRY_SLUGS[country])
    async with sem:
        try:
            async with session.get(url, headers={"User-Agent": UA}) as resp:
                if resp.status != 200:
                    return country, [], f"HTTP {resp.status}"
                payload = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001
            return country, [], f"{type(err).__name__}: {err}"
    tuples: list[tuple[str, ...]] = []
    for warning in payload.get("warnings") or []:
        infos = (warning.get("alert") or {}).get("info") or []
        if infos:
            tuples.append(tuple(str(i.get("language") or "").strip() for i in infos))
    if verbose:
        print(f"  meteoalarm:{country}: {len(tuples)} docs", file=sys.stderr)
    return country, tuples, None


# ---------------------------------------------------------------------------
# Selection rules under test
# ---------------------------------------------------------------------------


def _primary_index(langs: tuple[str, ...], preferred: str) -> int:
    """The shared shape of both providers' primary ladders.

    Exact/primary-subtag match, then English, then document order. Close enough
    to both ``wmo._select_info`` and ``meteoalarm._pick_info_blocks`` for a
    survey of which block ends up as the *alternate*.
    """
    if preferred:
        for idx, lang in enumerate(langs):
            if lang.lower() == preferred.lower():
                return idx
        for idx, lang in enumerate(langs):
            if _primary_subtag(lang) == _primary_subtag(preferred):
                return idx
    for idx, lang in enumerate(langs):
        if _primary_subtag(lang) == "en":
            return idx
    return 0


def _alt_before(langs: tuple[str, ...], primary: int) -> int | None:
    """The pre-#154 rule both providers had: the first leftover block."""
    for idx in range(len(langs)):
        if idx != primary:
            return idx
    return None


def _alt_shipped(langs: tuple[str, ...], primary: int) -> int | None:
    """The rule in the tree, so the survey measures what actually ships."""
    return alternate_info_index(langs, primary)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _report(provider: str, per_scope: dict[str, list[tuple[str, ...]]]) -> None:
    docs = [t for ts in per_scope.values() for t in ts]
    print(f"\n== {provider}: {len(docs)} documents across {len(per_scope)} scopes ==")

    distinct_counts = Counter(len({_primary_subtag(x) for x in t}) for t in docs)
    print("\nDistinct languages per document:")
    for n in sorted(distinct_counts):
        print(f"  {n}: {distinct_counts[n]}")

    multi_scopes = sorted(
        sid
        for sid, ts in per_scope.items()
        if any(len({_primary_subtag(x) for x in t}) > 1 for t in ts)
    )
    print(f"\nScopes publishing >1 language: {len(multi_scopes)}")

    print("\n3+-language combinations (ordered as published; scopes):")
    combos: dict[tuple[str, ...], set[str]] = defaultdict(set)
    combo_docs: Counter[tuple[str, ...]] = Counter()
    for sid, ts in per_scope.items():
        for t in ts:
            if len({_primary_subtag(x) for x in t}) >= 3:
                combos[t].add(sid)
                combo_docs[t] += 1
    if not combos:
        print("  none")
    for t, sids in sorted(combos.items(), key=lambda kv: -combo_docs[kv[0]]):
        has_en = any(_primary_subtag(x) == "en" for x in t)
        print(
            f"  {combo_docs[t]:4d}  {'/'.join(t)}  "
            f"{'en present' if has_en else 'NO ENGLISH'}  {sorted(sids)}"
        )

    # Simulation: every preference a user could set on this document.
    disagree = 0
    considered = 0
    non_en_alt_before = 0
    non_en_alt_shipped = 0
    examples: list[str] = []
    for sid, ts in per_scope.items():
        for t in ts:
            if len({_primary_subtag(x) for x in t}) < 2:
                continue
            prefs = sorted({x for x in t if x}) + ["en"]
            for pref in prefs:
                p = _primary_index(t, pref)
                before = _alt_before(t, p)
                shipped = _alt_shipped(t, p)
                if before is None:
                    continue
                considered += 1
                if _primary_subtag(t[p]) != "en":
                    if _primary_subtag(t[before]) != "en":
                        non_en_alt_before += 1
                    if shipped is not None and _primary_subtag(t[shipped]) != "en":
                        non_en_alt_shipped += 1
                if before != shipped:
                    disagree += 1
                    if len(examples) < 12:
                        shipped_tag = t[shipped] if shipped is not None else "(none)"
                        examples.append(
                            f"    {sid} pref={pref}: primary={t[p]} "
                            f"before_alt={t[before]} shipped_alt={shipped_tag}"
                        )
    print(
        f"\nSimulation over {considered} (document × preference) pairs on "
        "multilingual documents:"
    )
    print(f"  pre-#154 rule != shipped rule: {disagree}")
    print(
        "  non-English primary with non-English alternate: "
        f"before {non_en_alt_before}, shipped {non_en_alt_shipped}"
    )
    if examples:
        print("  examples of disagreement:")
        print("\n".join(examples))


async def _run(args: argparse.Namespace) -> int:
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    sem = asyncio.Semaphore(args.concurrency)
    results: dict[str, dict[str, list[tuple[str, ...]]]] = {}
    errors: dict[str, str] = {}
    started = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if "wmo" in providers:
            sources = await fetch_wmo_sources(session, user_agent=UA)
            if args.wmo_limit is not None:
                sources = sources[: args.wmo_limit]
            print(f"wmo: {len(sources)} sources", file=sys.stderr)
            rows = await asyncio.gather(
                *(
                    _wmo_source_docs(session, sid, args.wmo_docs, sem, args.verbose)
                    for sid, _ in sources
                )
            )
            results["wmo"] = {sid: ts for sid, ts, _ in rows}
            errors.update({f"wmo:{sid}": e for sid, _, e in rows if e})
        if "meteoalarm" in providers:
            countries = sorted(METEOALARM_COUNTRIES)
            print(f"meteoalarm: {len(countries)} countries", file=sys.stderr)
            rows = await asyncio.gather(
                *(
                    _meteoalarm_country_docs(session, c, sem, args.verbose)
                    for c in countries
                )
            )
            results["meteoalarm"] = {c: ts for c, ts, _ in rows}
            errors.update({f"meteoalarm:{c}": e for c, _, e in rows if e})

    elapsed = time.monotonic() - started
    if args.json:
        json.dump(
            {
                "elapsed_s": round(elapsed, 1),
                "results": {
                    p: {sid: [list(t) for t in ts] for sid, ts in scopes.items()}
                    for p, scopes in results.items()
                },
                "errors": errors,
            },
            sys.stdout,
            indent=1,
        )
        return 0
    for provider, per_scope in results.items():
        _report(provider, per_scope)
    if errors:
        print(f"\n{len(errors)} scope errors:")
        for label, err in sorted(errors.items()):
            print(f"  {label}: {err}")
    print(f"\nelapsed {elapsed:.0f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--providers", default="wmo,meteoalarm")
    parser.add_argument("--wmo-docs", type=int, default=8, help="CAP bodies per source")
    parser.add_argument("--wmo-limit", type=int, default=None, help="first N sources")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
