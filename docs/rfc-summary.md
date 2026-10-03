# The `incident` RFC on one page

A summary of [`rfc.md`](../rfc.md), a public working draft not yet submitted to
the Home Assistant Architecture repository. The RFC is the source of truth, and
the measurements stay there and in [`evidence/`](evidence/README.md).

## Packed alert sensors lose data as the situation gets worse

Home Assistant caps an entity's attributes at 16 KB. A sensor that packs every
active alert into its attributes grows with the alert count, and on overflow
the recorder keeps the state and silently drops the attributes (§1.1).
Providers also re-issue one alert under new ids, which splits its history
(§1.2), and no two integrations share a severity or identity vocabulary (§1.3).

## Two separable claims

**The abstraction.** Home Assistant needs a first-class incident, and §1.4
states what that takes without assuming a binding:

| # | Requirement | In one line |
| :-- | :-- | :-- |
| 1 | Normalized vocabulary | CAP fields are normalized once, centrally |
| 2 | Stable lifecycle identity | One incident, one identity across re-issues |
| 3 | Bounded footprint | Heavy payloads are referenced, not inlined |
| 4 | Concurrent multiplicity | Many incidents coexist without dropout |
| 5 | Restart survival | On HA-native persistence, without disk wear |
| 6 | Dynamic active set | Items leave on cancel or expiry |
| 7 | Automation surface | Triggers on arrival, update and termination |
| 8 | Tolerance of imperfect sources | One missed observation ends nothing |
| 9 | Ingest-mode neutrality | Holds for polling and for a pushed stream |
| 10 | Readable by a dashboard | A subscribed read path that cards can use |

The pattern core review currently prefers, a count entity plus an action that
returns the alert bodies, fails requirement 10 (§1.6).

**The binding.** One `incident.*` entity per active incident, created and
removed with it (§2). State is the normalized severity, three lifecycle events
share one payload (§2.3), and geometry is fetched on demand through a handle
(§2.4). Absence is not termination: an incident missing from the feed but
still inside its published expiry is kept and marked stale (§2.5). Entities
inherit the recorder, the trigger editor and the card ecosystem, at the cost of
batched registry churn (§2.5). A static entity pool (§6.1) and a dedicated
registry (§3.6) are the alternatives, and the contract ports unchanged to
either.

Accepting the first claim and rejecting the second is not rejecting the
proposal.

## What runs today, and the gap

The [`cap_alerts`](../README.md) custom integration implements everything in §2
except restart content restore, across weather, civil-protection and disaster
feeds (§4.1). The entities don't inherit `RestoreEntity` yet, so that half of
§2.5 is specified and not field-tested (§5).

## What I'm asking for

Test the requirements in §1.4 first, then argue with the binding. An issue on
this repository is the place for either.
