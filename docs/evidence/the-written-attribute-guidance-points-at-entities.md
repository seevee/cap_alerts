# The written guidance on attributes points at entities, not actions

| | |
| :-- | :-- |
| Supports | RFC §1.6, §1.5, §8.1 |
| Source | [`docs/core/entity.md`](https://developers.home-assistant.io/docs/core/entity#generic-properties) on `home-assistant/developers.home-assistant`, its [PR #1245](https://github.com/home-assistant/developers.home-assistant/pull/1245); [home-assistant/core#109142](https://github.com/home-assistant/core/pull/109142) and [#109152](https://github.com/home-assistant/core/pull/109152); three Home Assistant Discord messages returned by a search for `extra_state_attributes` |
| Sample | 1 documentation page, 1 docs PR, 2 core PRs, 3 Discord messages, 2022-03-21 to 2024-03-15 |
| Observed | 2026-10-27 (docs read from `master`, PR threads fetched with `gh api`, Discord rows pasted from a signed-in client, times converted from the reader's zone) |
| Reproduce | `timeout 30 gh api -H 'Accept: application/vnd.github.raw' repos/home-assistant/developers.home-assistant/contents/docs/core/entity.md` and `timeout 30 gh api repos/home-assistant/core/pulls/109142/comments` |

The developer documentation says two things about attributes, both on the
entity page. The property table, present for as long as the page's history
goes back:

> `extra_state_attributes`: Extra information to store in the state machine.
> It needs to be information that further explains the state, it should not
> be static information like firmware version.

and a warning block added by @bdraco in PR #1245, merged 2022-03-21:

> Entities that generate a significant amount of state changes can quickly
> increase the size of the database when the `extra_state_attributes` also
> change frequently. Minimize the number of `extra_state_attributes` for
> these entities by removing non-critical attributes or creating additional
> `sensor` entities.

Neither mentions actions. The remedy named is more entities.

| When (UTC) | Who | Where | Quote |
| :-- | :-- | :-- | :-- |
| 2022-09-13 11:43 | aspec | Discord | "Based on the dev docs, I'm assuming it's preferred to move these out as separate entities but is there a general rule of thumb as to what's acceptable as an extra state attribute vs creating a new entity?" |
| 2024-01-30 15:57 | @frenck | [core#109142](https://github.com/home-assistant/core/pull/109142#discussion_r1471501024) | "Why should these be state attributes and not separate states?" |
| 2024-01-30 16:25 | @frenck | [core#109142](https://github.com/home-assistant/core/pull/109142#discussion_r1471551323) | "These are pretty old constructs. We avoid the use of attributes nowadays." |
| 2024-01-30 16:27 | @jrieger | [core#109142](https://github.com/home-assistant/core/pull/109142#discussion_r1471555004) | "Changing all sensors to move all state attributes to separate states would be a breaking change, though. What do you think?" |
| 2024-01-30 16:33 | Higilopochtli | Discord | "I'll need some opinion here: [that thread]. An option would be to move all/most of those to separate states, but this will be a breking change. Is that the way to go?" |
| 2024-01-30 18:22 | @jrieger | core#109142 | "Closing in favour of #109152" |
| 2024-01-30 18:36 | @frenck | [core#109152](https://github.com/home-assistant/core/pull/109152), `APPROVED` | "Thanks, @jrieger 👍" |
| 2024-03-15 07:52 | ashgittins | Discord | "The docs at [the entity page] warn that if a sensor state changes often and extra_state_attributes changes often it can lead to increased DB writes, and that moving attributes out to their own sensors can solve that." |

#109142, "Add missing extra state attributes to QNAP", added two network
counters as attributes beside an existing one. #109152, "Remove unused
constants from QNAP", merged 2024-01-31 and added nothing. The attributes
were dropped, not moved.

## Reading

§1.6 says no ADR, quality-scale rule or developer-docs statement recommends
actions over attributes. That stays true. What the docs do say, since 2022,
is that attributes exist to explain the state and that a busy entity should
shed them into further `sensor` entities. The project lead applied it on
review in 2024 as "we avoid the use of attributes nowadays", and the
direction asked for was separate states.

So there are two conventions, not one. The written one, attributes to
entities, is the direction the §2 binding takes: an alert's fields become an
entity of their own rather than a list on a count sensor. The unwritten one,
attributes to actions, is the newer layer that
[core review applies to alert bodies](core-review-moved-alert-bodies-into-actions.md),
and it is the only one of the two that gives a dashboard nothing to
subscribe to. The §2 binding is on the side of the documented rule; the
objection it meets is the undocumented one.

Read with the [dynamic-entities page](core-review-calls-dynamic-entities-bad-practice.md),
the two pull against each other. The docs say shed attributes into
entities; review says do not mint entities by content count. A set whose
size the content decides cannot satisfy both, and neither statement was
written with that set in mind. That is the inconsistency the RFC asks the
AWG to resolve, not a rule it can claim to be following.

Two developers on Discord, in 2022 and 2024, read the warning as a rule to
follow and asked where its line falls. Nobody answered either of them in
the captured results.

## Caveats

- The property-table sentence is older than every commit the API lists for
  the page, so its author and date were not recovered. The warning block's
  are.
- The warning is about database growth from frequently changing attributes
  on frequently changing entities. An alert entity changes rarely, so the
  warning's motive does not apply to it; what is cited is the remedy it
  names.
- "We avoid the use of attributes nowadays" was said about two packet
  counters on a NAS sensor, and the resolution was to drop them. It is a
  statement of practice, not a ruling on alert bodies.
- The Discord rows have no permalinks; they were returned by a search for
  `extra_state_attributes` and copied by hand. Times are the client's
  display in the reader's zone (MDT, or MST for the January row) converted
  by hand. The bracketed links in two quotes replace pasted URLs.
- The 2024-01-30 Discord row is the same thread brought to chat; it is
  listed to show how the review reached Discord, not as a second source.
