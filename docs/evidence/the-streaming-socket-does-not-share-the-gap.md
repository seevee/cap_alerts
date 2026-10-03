# The NAAD streaming socket does not share the GeoRSS index gap

| | |
| :-- | :-- |
| Supports | RFC §2.5 (reconciliation, not poll, and ingest neutrality) |
| Source | `streaming.alertready.ca:8443` held open, both GeoRSS hosts fetched every 15 min |
| Sample | one socket session, 7.5 days to 2026-08-21, 736 index samples from 2026-08-14 |
| Observed | 2026-08-21 |
| Reproduce | one-time capture; no snapshot committed. A new run is `scripts/naad_stream_diff.py --log stream.jsonl`, then `--summary stream.jsonl` |

| Measure | Value | Where recorded |
| :-- | --: | :-- |
| Alerts the alertready index dropped while the socket was up, delivered by the socket | 75 of 75 | probe output, not committed |
| Socket connects over the session | 6 in 7.5 days | [#164](https://github.com/seevee/cap_alerts/issues/164) body |
| Total socket downtime | 32.7 s | #164 body |
| Alerts missed by the socket | 1, an "ended" bulletin sent into a ~6 s reconnect window on 2026-08-14 | #164 body |
| GeoRSS gap-sampler samples showing a gap, 2026-07-30 to 2026-08-21 | 100% | [#163](https://github.com/seevee/cap_alerts/issues/163) body |
| Alerts streamed in the last week of the run | 658 | #164 comment, 2026-08-22 |

The heartbeat the recovery path reads, as the probe confirmed it (#164
comment, 2026-08-22):

| Heartbeat `<references>` shape | Measured |
| :-- | :-- |
| Entries per heartbeat | 10 `sender,identifier,sent` triples |
| Repository URL rule `cap.alertready.ca/{YYYY-MM-DD}/{sent}I{identifier}.xml`, with `:` `-` `+` folded to `_` | matched 372 of 372 CAP links on the index |
| References from the first heartbeat fetched by that rule | 10 of 10 HTTP 200, right `<identifier>` in the body |
| Alerts already in the repository when they arrived on the socket | 3 of 3 |
| Two alerts the alertready index omitted, fetched by constructed URL | 2 of 2 HTTP 200 (56 KB and 32 KB) |

```
<sender>NAADS-Heartbeat</sender>      status System, at least every 60 s
<references>sender,identifier,sent sender,identifier,sent …</references>
```

## Reading

The index generator drops alerts that the same operator's socket and
repository both serve, so the defect is the index, not ingest. A streaming
provider therefore reaches a complete active set without the index, which is
what lets §2.5 state its rules against "reconciliation" rather than a poll.
The socket's only blind spot is its own downtime, and the heartbeat names the
last ten alerts so a reconnect can recover them by reference.

## Caveats

- The 75 of 75 figure lives only in the probe's console output. The JSONL log
  was not committed, so the number cannot be re-derived from the repo.
- One session from one network. Reconnect frequency depends on the client's
  connection, not only the server.
- The heartbeat window is ten alerts. An outage longer than ten national
  alerts cannot be recovered by reference, and nothing surviving enumerates
  the repository (#164 comment).
- The shape block above is a schematic from the module docstrings
  (`providers/naad_stream.py`, `providers/eccc.py`
  `async_fetch_docs_by_reference`), not a copied heartbeat.
