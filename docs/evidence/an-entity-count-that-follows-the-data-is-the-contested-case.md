# An entity count that follows the data is the contested case, a fixed shape is not

| | |
| :-- | :-- |
| Supports | RFC §1.5, §1.6, §6.1 |
| Source | Home Assistant Discord: [a channel conversation of 2026-04-05](https://discord.com/channels/330944238910963714/554842238073700352/1490502926366412870) on a first-integration PR; [the thread "Mealie Image Proxy", opened 2026-05-11](https://discord.com/channels/330944238910963714/1503359432300433508/1503408294235078696), on [home-assistant/core#166202](https://github.com/home-assistant/core/pull/166202); the PR itself; a client search for "dynamic entity" across the server |
| Sample | 2 Discord threads and 1 PR, 2026-04-05 to 2026-05-24; 6 search hits, 2024-12-26 to 2026-09-30 |
| Observed | 2026-10-27, read in a signed-in Discord client |
| Reproduce | Open the permalinks in a Discord client signed in to the Home Assistant server. `timeout 30 gh api repos/home-assistant/core/issues/166202/comments` for the PR comment. No script re-derives a Discord conversation |

Chat is paraphrased here and linked, not quoted. Speakers are named by role.

## The question, as core review met it

core#166202 adds an unauthenticated view so that Mealie's `get_mealplan` and
`get_recipe` responses carry an image URL a card can use. On 2026-05-11 the
`mealie` code owner asked in Discord whether core wants it at all. The
replies objected to the implementation: an unauthenticated view keyed only
on the entry id, and transport code calling the aiohttp session directly,
which core does not allow. Returning usable data from the action was
welcomed.

Another member suggested an `image` entity, disabled by default, instead of
the view. The code owner answered that a week's meal plan can hold one
recipe or fifty, so the entities would have to be created to match, and
called dynamic entities bad practice
([message](https://discord.com/channels/330944238910963714/1503359432300433508/1503408294235078696)).
The member who suggested it had assumed a fixed set, like the per-list
entities Mealie already creates for shopping lists and calendars. Told
otherwise, that member judged the feature fine and the implementation not.
The author closed the PR on 2026-05-24 without comment, and the code owner
[noted on the PR](https://github.com/home-assistant/core/pull/166202#issuecomment-4528567027)
that opinions had been sought and the feedback settled it.

The word "dynamic" appears nowhere on #166202. The entity remark is a side
answer to a side suggestion, not the verdict on the PR.

## The earlier statement

On 2026-04-05 the same reviewer advised a first-integration author who
wanted one sensor per value a device reports, with the entity description
chosen by the unit the device sends
([message](https://discord.com/channels/330944238910963714/554842238073700352/1490502926366412870)).
The objection was checkability: entities discovered from a payload are hard
to verify, and a boolean would land as a `sensor` rather than a
`binary_sensor`. The advice was to start with a few named sensors. The author
agreed to a fixed set.

## Outside core

Six search hits state a position on creating entities. Four are one
recurring conversation, 2024-12 to 2026-09, about voice-satellite timers:
n of them run at once and each ends on its own, and the ESPHome side said
three times that HA entities cannot carry that. The one core-side answer
offered a new platform or dynamic creation. Neither shipped.

## Reading

§6.1 opens with "If the AWG rejects dynamic creation". The Mealie thread is
the first record of anyone in core holding that position, from the reviewer
whose single review closed the ECCC attribute PR
([evidence](core-review-moved-alert-bodies-into-actions.md)). It is one
reviewer's practice on two threads, stated as an opinion with no ADR or
quality-scale rule behind it. That is the §1.6 point, applied to the binding
rather than the payload.

The thread also draws the line. Per-list and per-calendar entities passed
without objection. What was refused is a set whose size the content decides
on a given day, which is the active alert set exactly. Hence §6.1, and
§2.5's figure per entry scope rather than a constant.

The April objection is a different one. It is about an entity whose type is
discovered from a payload. The §2 binding is dynamic in count and fixed in
shape: every incident entity has the same state vocabulary and attribute set
(§2.1), so that objection is answered by construction.

The timer conversation shows the belief held outside core as received
wisdom. Core does ship one entity per feed item where the domain is
`geo_location` (`gdacs`, `nsw_rural_fire_service`, §3.7). The objection is
to a data-sized set arriving in `sensor` or `image`, which points at §1.5's
second column, an `incident` domain, if the first column is refused.

## Caveats

- Discord is the source for every statement that matters. The server is
  open to join, but the permalinks need a signed-in client.
- The search hits have no permalinks or surrounding context, so they are
  reported as a count and a topic only.
- The April thread names its PR as `pull/44504`, which does not resolve to
  that PR on `home-assistant/core` and is not cited.
