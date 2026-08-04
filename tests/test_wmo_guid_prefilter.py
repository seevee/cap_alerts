"""RSS-envelope area-code pre-filter and the byte-bounded body cache.

Both exist for one failure: cn-cma-xx publishes 501 CAP URLs per poll at
~89 KiB each, which cost ~32 s against a 30 s timeout, so the entry never
completed an update and stale entities were never removed.

The pre-filter is an optimization under the authoritative post-fetch geocode
filter, so every test here is really asking the same question — can it ever
drop an alert the post-filter would have kept? It must not.
"""

from __future__ import annotations

import sys

import pytest

from custom_components.cap_alerts.providers.cap_content_cache import CAPContentCache
from custom_components.cap_alerts.providers.wmo import _parse_rss_links

_ITEM = """
  <item>
    <title>{title}</title>
    <link>https://example.invalid/cap/{guid}.xml</link>
    <guid isPermaLink="false">{guid}</guid>
  </item>
"""


def _feed(*guids: str) -> str:
    items = "".join(_ITEM.format(guid=g, title=g) for g in guids)
    return f"<rss><channel>{items}</channel></rss>"


# Real cn-cma-xx guid shape: <6-digit GB/T 2260 code><serial>_<timestamp>.
_HEBEI = "13070941600000_20260804103516"
_GUIZHOU = "52272741600000_20260804103516"
_BEIJING = "11010541600000_20260804103516"


def _kept(xml: str, prefixes=None) -> list[str]:
    return _parse_rss_links(xml, geocode_prefixes=prefixes)


# ---------------------------------------------------------------------------
# The optimization
# ---------------------------------------------------------------------------


def test_prefix_skips_non_matching_items_before_any_fetch():
    links = _kept(_feed(_HEBEI, _GUIZHOU, _BEIJING), ["13"])
    assert len(links) == 1
    assert _HEBEI in links[0]


def test_multiple_prefixes_union():
    links = _kept(_feed(_HEBEI, _GUIZHOU, _BEIJING), ["13", "11"])
    assert len(links) == 2


def test_six_digit_prefix_selects_one_county():
    links = _kept(_feed(_HEBEI, _GUIZHOU, _BEIJING), ["130709"])
    assert len(links) == 1
    assert _HEBEI in links[0]


def test_no_prefix_configured_keeps_everything():
    assert len(_kept(_feed(_HEBEI, _GUIZHOU, _BEIJING))) == 3
    assert len(_kept(_feed(_HEBEI, _GUIZHOU), [])) == 2


# ---------------------------------------------------------------------------
# Fail-open guards — each of these MUST return the full set, because the guid
# cannot answer the question and the post-fetch filter has to decide instead.
# ---------------------------------------------------------------------------


def test_prefix_longer_than_the_embedded_code_disengages():
    """The regression this guard exists for.

    A full 12-digit code matches the CAP body's geocode ``130709000000`` but
    never the guid's ``13070941600000`` — digits 7+ are the serial. Filtering
    on the guid would drop the very alert the user asked for.
    """
    assert len(_kept(_feed(_HEBEI, _GUIZHOU), ["130709000000"])) == 2


def test_non_numeric_prefix_disengages():
    # UGC/EMMA_ID-style prefixes say nothing about a numeric guid.
    assert len(_kept(_feed(_HEBEI, _GUIZHOU), ["OHZ"])) == 2


def test_mixed_safe_and_unsafe_prefixes_disengage_entirely():
    assert len(_kept(_feed(_HEBEI, _GUIZHOU), ["13", "130709000000"])) == 2


def test_a_feed_whose_guids_are_not_area_codes_disengages():
    other = _feed("urn:oid:2.49.0.1.124.x.y", "tag:example.invalid,2026:1")
    assert len(_kept(other, ["13"])) == 2


def test_one_unparseable_guid_disengages_for_the_whole_feed():
    """Partial application would silently drop the odd item out."""
    mixed = _feed(_HEBEI, "not-a-code", _GUIZHOU)
    assert len(_kept(mixed, ["13"])) == 3


def test_zero_matches_fails_open_rather_than_emptying_the_entry():
    """A guid convention change must not look like 'no alerts in your area'."""
    assert len(_kept(_feed(_HEBEI, _GUIZHOU), ["99"])) == 2


def test_missing_guid_element_disengages():
    xml = "<rss><channel><item><link>https://e.invalid/a.xml</link></item></channel></rss>"
    assert len(_kept(xml, ["13"])) == 1


# ---------------------------------------------------------------------------
# Byte-bounded cache
# ---------------------------------------------------------------------------


def test_cache_evicts_by_bytes_not_entry_count():
    body = "x" * 10_000
    budget = 3 * sys.getsizeof(body)
    cache = CAPContentCache(max_bytes=budget)
    for i in range(10):
        cache._store(f"u{i}", body)
    assert cache.total_bytes <= budget
    assert len(cache) == 3


def test_cache_keeps_the_most_recently_stored():
    body = "y" * 10_000
    cache = CAPContentCache(max_bytes=2 * sys.getsizeof(body))
    for url in ("a", "b", "c"):
        cache._store(url, body)
    assert "a" not in cache._cache
    assert "b" in cache._cache and "c" in cache._cache


def test_restoring_the_same_url_does_not_double_count():
    body = "z" * 10_000
    cache = CAPContentCache(max_bytes=10 * sys.getsizeof(body))
    cache._store("same", body)
    first = cache.total_bytes
    cache._store("same", body)
    assert cache.total_bytes == first
    assert len(cache) == 1


def test_a_body_larger_than_the_budget_is_not_cached():
    """Caching it would evict everything, then be evicted itself."""
    cache = CAPContentCache(max_bytes=1024)
    cache._store("small", "a" * 100)
    before = cache.total_bytes
    cache._store("huge", "b" * 100_000)
    assert "huge" not in cache._cache
    assert cache.total_bytes == before


def test_default_budget_holds_the_cn_cma_xx_working_set():
    """501 bodies averaging 88.9 KiB — the case that regressed."""
    from custom_components.cap_alerts.providers.cap_content_cache import (
        DEFAULT_MAX_BYTES,
    )

    assert DEFAULT_MAX_BYTES >= 501 * 89 * 1024


@pytest.mark.parametrize("count", [1, 50, 501])
def test_working_set_survives_a_full_poll(count: int):
    """The actual regression: a poll must not evict its own earlier entries."""
    body = "c" * 89 * 1024
    cache = CAPContentCache()
    for i in range(count):
        cache._store(f"https://example.invalid/{i}.xml", body)
    assert len(cache) == count
