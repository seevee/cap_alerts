# Core review calls one entity per item bad practice when the count follows the data

| | |
| :-- | :-- |
| Supports | RFC §1.5, §1.6, §6.1 |
| Source | Home Assistant Discord: [a channel conversation of 2026-04-05](https://discord.com/channels/330944238910963714/554842238073700352/1490502926366412870) on a first-integration PR; [the thread "Mealie Image Proxy", opened 2026-05-11](https://discord.com/channels/330944238910963714/1503359432300433508/1503408294235078696), on [home-assistant/core#166202](https://github.com/home-assistant/core/pull/166202); the PR itself; a client search for "dynamic entity" across the server |
| Sample | 2 Discord threads and 1 PR, 2026-04-05 to 2026-05-24; 6 search hits, 2024-12-26 to 2026-09-30 |
| Observed | 2026-10-27 (transcripts and search results pasted from a signed-in Discord client; the two linked times recovered from the message ids, which carry a UTC timestamp) |
| Reproduce | Open the permalinks in a Discord client signed in to the Home Assistant server. `timeout 30 gh api repos/home-assistant/core/issues/166202/comments` for the PR comment. No script re-derives a Discord transcript |

## The thread

core#166202, "Add image proxy for Mealie recipe services" by @domodom30,
opened 2026-03-22, adds an unauthenticated view so that `get_mealplan` and
`get_recipe` responses carry an image URL a card can use. The code owner
opened a thread to ask whether core wants it at all. Times are UTC, from the
thread's and the quoted message's ids; the rest are the client's display
converted from the reader's zone.

| When (UTC) | Who | Quote |
| :-- | :-- | :-- |
| 2026-05-11 11:33 | @joostlek, `mealie` code owner | "I'd love an opinion on https://github.com/home-assistant/core/pull/166202, it adds an image proxy for Mealie. Would you add this? Why/why not?" |
| 11:56 | Markus | "Honestly not sure if using just entry_id aligned with good security practices on access control with an unauthenticated api." |
| 11:58 | @MartinHjelmare | "It also has transport code (using the aiohttp session directly to make requests) which we don't allow." |
| 11:59 | @joostlek | "I don't want to push back on a PR which we wouldn't want anyway" |
| 12:00 | @MartinHjelmare | "It's nice to be able to use the returned data directly. From a naive view point I think that's good. I think it comes down to the implementation." |
| 14:36 | @abmantis | "why not implement the image platform? with an entity disabled by default, since most users may not need it" |
| 14:38 | @joostlek | "because it's not a static image. this is like, I want to fetch a list of recipes for this week. Result can be 1 image, but can also be 50" |
| 14:46 | @abmantis | "ah. I thought the user had a list of recipes in their account that we could create entities dynamically for (like it is done for the shopping lists and calendars)" |
| 14:47 | @joostlek | "Nope, like in theory you could create an image enttiy for the recipes planned on a day, but there can be n recipes, so you'd have dynamic entities and I think that's a bad practice" ([message](https://discord.com/channels/330944238910963714/1503359432300433508/1503408294235078696)) |
| 14:49 | @abmantis | "in that case I think the feature is fine (but not the implementation)" |
| 2026-05-24 09:00 | @domodom30 | closed the PR, no comment |
| 2026-05-24 12:10 | @joostlek, on the PR | "Fyi I have been discussing this as I wasn't sure if this is a change we general want and I think I'm fine with the feedback I've gotten" ([comment](https://github.com/home-assistant/core/pull/166202#issuecomment-4528567027)) |

The word "dynamic" appears nowhere on #166202. The thread's verdict on the
PR was feature yes, implementation no, on the unauthenticated view and the
transport code. The entity line is a side answer: a reviewer offered an
`image` platform as the alternative and it was refused because the count
follows the meal plan.

## The earlier statement

A first-integration author asked, on 2026-04-05, about creating one sensor
per value a device reports, with the entity description picked by the unit
the device sends. The device renames its keys by UI language.

| When (UTC) | Who | Quote |
| :-- | :-- | :-- |
| 2026-04-06 00:02 | @joostlek | "But then we also can't really use it for the unique id" |
| 00:06 | @joostlek | "I'm generally not a big fan of just creating entities dynamically from the API, as it's hard to actually check if it's correct. For example how do we make sure we don't add values that we don't want in the state machine or what if there's a true false value, then that shouldn't be a sensor but a binary one" ([message](https://discord.com/channels/330944238910963714/554842238073700352/1490502926366412870)) |
| 00:10 | @joostlek | "For the initial PR i think its nice to just have a few, it's easier to get that in and add more later than to add everything right away most likely" |
| 00:12 | the author | "I was just wondering what the reason is to not have a sensor default to device_class TEMPERATURE and state_class MEASUREMENT if the unit is "°C" ? but I guess explicit is better than implicit" |

The author agreed to a fixed, named set ("I'll map all languages to english
in my library", "start with a few intresting sensors, not the whole kitchen
sink").

## Search results for "dynamic entity"

Of the hits the client returned, these state a position on entity creation.
The rest are dashboard and template questions. No permalinks were captured;
times are converted from the reader's display zone.

| When (UTC) | Who | Quote |
| :-- | :-- | :-- |
| 2024-12-25 22:04 | @joostlek | "We dynamically add entities based on the webhook data?" |
| 2024-12-26 15:45 | formatBCE (ESPHome) | "HA doesn't work well with dynamic entities - only with attributes." |
| 2024-12-26 15:51 | formatBCE | "I mean, you sorta need to create timer entity dynamically in HA when user sets new timer, and delete after completion. That's not how HA works.. 🙂" |
| 2025-05-28 12:11 | petro | "maybe a new timer event entity with the new entity in the attribute, then dynamically create time entities while also having a next timer entity?" |
| 2025-05-28 12:31 | petro | "Alright, then what? Something's gotta give, either a new platform or follow in line with current platforms, or dynamically created entities." |
| 2025-09-05 18:49 | formatBCE | "Since you can have multiple timers running, it would require dynamic entity creation and deletion (on timer run out)." |
| 2026-09-30 20:56 | formatBCE | "server would have to create/delete timer entities dynamically, which isn't the way it works currently on HA" |

## Reading

§6.1 opens with "If the AWG rejects dynamic creation" and, until this page,
nothing in the evidence set showed that anyone in core holds that position.
The Mealie thread is the first recorded statement of it, from the reviewer
whose single review closed the ECCC attribute PR
([evidence](core-review-moved-alert-bodies-into-actions.md)). The form is
the GeoSphere code owner's: "I think that's a bad practice", with no ADR or
quality-scale rule behind it. That is the §1.6 point, applied to the binding
rather than the payload.

The thread also draws the line. Mealie already creates entities per shopping
list and per meal-plan calendar, and the reviewer who named them was not
contradicted. What was refused is a set whose size is decided by the content
on a given day, "1 image, but can also be 50". An active alert set is that
case exactly, which is why §6.1 exists and why the figure in §2.5 is per
entry scope, not a constant.

The April quote objects to something else: an entity whose type is
discovered from a payload is "hard to actually check", a boolean might land
as a `sensor`. The binding in §2 is dynamic in count and fixed in shape.
Every incident entity has the same state vocabulary and attribute set
(§2.1), so that objection is answered by construction.

The search results show the belief held outside core as received wisdom.
Voice-satellite timers are n concurrent, created on start and deleted on
run-out, and the ESPHome side has said three times across two years that HA
cannot carry them as entities; the core answer offered was a new platform or
dynamic creation, and neither shipped. Core does ship one entity per feed
item where the domain is `geo_location` (`gdacs`, `nsw_rural_fire_service`,
§3.7). The objection is to a data-sized set arriving in `sensor` or `image`,
which points at §1.5's second column, an `incident` domain, as the answer to
raise if the first column is refused.

## Caveats

- Discord is the source for every statement that matters. The server is
  open to join, but the permalinks need a signed-in client, and no command
  in this repo re-derives a transcript. Quotes were copied by hand on
  2026-10-27, typos kept, three multi-line messages joined with full stops.
- Two messages carry UTC from their ids. Every other time is the client's
  display in the reader's zone (MDT, or MST for the December rows)
  converted by hand.
- The search rows have no permalinks and no surrounding context. The timer
  rows are three people in what reads as one recurring conversation; the
  2024-12-25 @joostlek line is quoted because the search returned it, its
  context was not captured and it may be about a different proposal.
- The April thread's PR number as given in the thread (`pull/44504`) does
  not resolve to that PR on `home-assistant/core` and is not cited.
- Markus and petro are quoted by display name; their GitHub logins were not
  confirmed.
- Quotes are trimmed to the sentences that carry the point. The 14:47Z and
  00:06Z messages are quoted in full.
