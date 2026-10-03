# NWS zone geometry is heavy-tailed, with a 1,700x span inside one population

| | |
| :-- | :-- |
| Supports | RFC §2.4, §6.2, §6.6 |
| Source | NWS bulk zone shapefiles (public forecast zones, counties, marine zones), the shapes a zone-based `api.weather.gov` alert resolves to |
| Sample | 11,888 zone records, 8,975,685 coordinate pairs, every zone the shapefiles enumerate |
| Observed | 2026-08 (one-time census, day not recorded) |
| Reproduce | one-time census from the NWS bulk shapefiles, no snapshot committed. Shipped geometry round-trips through `scripts/geometry_conformance.py` against a running instance |

| Statistic | Value |
| :-- | --: |
| Zone records | 11,888 |
| Coordinate pairs, all zones | 8,975,685 |
| Median zone | ~190 points (~2 KB serialized) |
| 75th percentile | under 400 points |
| Largest zone, `AKC198` (Prince of Wales-Hyder, Alaska) | 93,667 points (~3/4 MB) |
| Span, smallest to largest | ~1,700x |
| `AKC198` simplified to a 400-point budget | 234x fewer points |

| Delivery path | Bytes | Requests |
| :-- | --: | --: |
| Bundled artifact, land zone types only, Douglas-Peucker tolerance 0.005 | 4.91 MB gzipped | 0 at render time |
| On demand, cold nationwide render (every zone a national alert set referenced) | ~1.78 MB | 265 |
| On demand, one home viewport (~3 zones) | ~20 KB | ~3 |

## Reading

An entry-count cap sized to the median admits a worst case three orders of
magnitude larger. The tail is Alaskan and coastal zones, which are the marine
and winter alerts a user there cares about most. That is why §2.4 bounds the
geometry store in bytes and never in entries. The second table is §6.6. The
precomputed artifact costs 2.8x the bytes of the worst case it was built for.
Simplify per response and do not ship the corpus. §6.2 reads the same numbers
from the other side. Per-incident reuse saves kilobytes, and 1.78 MB is the
ceiling on what a cross-integration cache can save in one session.

## Caveats

- NWS only. The census characterizes NWS forecast-zone geometry, not CAP
  polygons at large. What generalizes is the shape, not the numbers.
- One-time census. No script or data snapshot is committed, so the figures
  above cannot be re-derived from the repository.
- Sampling cannot stand in for the census. A 115-zone sample got per-type
  means wrong by 6.4x for counties, 2.5x for public zones and 49x for offshore
  zones.
- Gzip on compact four-decimal coordinate JSON ran about 4:1. The ~10:1 seen
  against `api.weather.gov` comes from that API pretty-printing its responses.
- The byte figures marked `~` are as recorded in the notes, not re-measured.
- Zone boundaries are revised, so a census taken later will differ in detail.
