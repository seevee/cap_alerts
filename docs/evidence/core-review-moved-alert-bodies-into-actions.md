# Core review moved alert bodies out of attributes and into actions

| | |
| :-- | :-- |
| Supports | RFC §1.6, §8.1 |
| Source | [home-assistant/core#164481](https://github.com/home-assistant/core/pull/164481), [#172393](https://github.com/home-assistant/core/pull/172393), [#161882](https://github.com/home-assistant/core/pull/161882), [#166125](https://github.com/home-assistant/core/pull/166125), [#178358](https://github.com/home-assistant/core/pull/178358), [#178474](https://github.com/home-assistant/core/pull/178474), [home-assistant/discussions#3130](https://github.com/orgs/home-assistant/discussions/3130), `homeassistant/components/nina/` and `homeassistant/components/geosphere_austria_warnings/` on core `dev`, [a7rk/core:geosphere-austria-warnings-add-service](https://github.com/a7rk/core/tree/geosphere-austria-warnings-add-service) |
| Sample | 6 PRs, 1 discussion and 1 unfiled fork branch, 2026-01-29 to 2026-09-25 |
| Observed | 2026-10-02 (ECCC and NINA threads) and 2026-10-11 (GeoSphere threads), fetched with `gh api`, thread dates as GitHub records them |
| Reproduce | `timeout 30 gh api repos/home-assistant/core/pulls/164481/reviews` (full command list below) |

| PR | Author | Opened | Outcome | Where the alert body ended up |
| :-- | :-- | :-- | :-- | :-- |
| #164481 Expose richer alert data and combine alert sensors in Environment Canada | @michaeldavie | 2026-02-28 | closed unmerged 2026-06-08 | attributes (`alerts` list), rejected |
| #172393 Environment Canada integration: add get_alerts action | @gwww | 2026-05-28 | merged 2026-06-08 | `environment_canada.get_alerts` response |
| #161882 Replace NINA attributes with sensors | @DeerMaximum | 2026-01-29 | merged 2026-04-01 | eight per-field sensors, attributes deprecated |
| #166125 Use actions in NINA to allow accessing data | @DeerMaximum | 2026-03-21 | merged 2026-07-10 | `nina.get_details` response |
| #178358 Add DWD style warning sensors to GeoSphere Austria warnings | @a7rk | 2026-08-06 | closed unmerged 2026-09-25, superseded by #178474 | attributes (`warning_<n>_*` slots), rejected by the code owner |
| #178474 Geosphere austria add advance warnings sensors | @a7rk | 2026-08-07 | merged 2026-09-25 | two enum level sensors carrying `type`, `level`, `start`, `end`, `warning_id` of the top-ranked warning; free text deferred to a `geosphere_austria_warnings.get_warnings` action that sits on the author's fork branch, unfiled (last commit 2026-08-07) |

| Date | Who | Quote | Link |
| :-- | :-- | :-- | :-- |
| 2026-03-03 | @joostlek, `CHANGES_REQUESTED` | "I would argue that we shouldn't store this in extra state attributes, but instead use actions with return values to return a list of all the alerts" | [review](https://github.com/home-assistant/core/pull/164481#pullrequestreview-3884628991) |
| 2026-03-03 | @michaeldavie | "Sorry, I don't know what you mean. How would you see this working?" | [comment](https://github.com/home-assistant/core/pull/164481#issuecomment-3994291164) |
| 2026-03-04 | @gwww | "@joostlek wondering if you help us understand the concern around storing the alerts in the extra state attributes." | [comment](https://github.com/home-assistant/core/pull/164481#issuecomment-4000851669) |
| 2026-03-13 | @seevee | "there's currently no way for cards to call actions and display return values" | [comment](https://github.com/home-assistant/core/pull/164481#issuecomment-4051633485) |
| 2026-04-02 | @seevee | "If not, what's the intended way for dashboards to access this data today?" | [comment](https://github.com/home-assistant/core/pull/164481#issuecomment-4173891889) |
| 2026-06-08 | @gwww | "Now that https://github.com/home-assistant/core/pull/172393 is merged, I recommend that this PR gets closed." | [comment](https://github.com/home-assistant/core/pull/164481#issuecomment-4650055767) |
| 2026-06-09 | @gwww, code owner, on discussions#3130 | "Perhaps not the ideal solution but the majority of users now have a path forward without a custom integration. For me it was choosing pragmatism over being right." | [comment](https://github.com/home-assistant/feature-requests/discussions/3130#discussioncomment-17230676) |
| 2026-08-06 | @tklecka, code owner, on #178358 | "Thanks for this — the gap is real. Our two summary sensors don't carry enough to build a useful warning card, and I want that fixed." | [comment](https://github.com/home-assistant/core/pull/178358#issuecomment-5205493570) |
| 2026-08-06 | @tklecka | "To be straight with you about where this objection comes from: there's no rule against them, and `dwd_weather_warnings` still ships exactly this shape on `dev` today. So this is my call as code owner, not a guideline I can point at." | [comment](https://github.com/home-assistant/core/pull/178358#issuecomment-5205493570) |
| 2026-08-06 | @tklecka | "The indices aren't stable. They're positional over a filtered, sorted list. When warning 1 expires, everything shifts up and `warning_2_level` silently starts describing a different warning." | [comment](https://github.com/home-assistant/core/pull/178358#issuecomment-5205493570) |
| 2026-08-06 | @tklecka | "That's the direction I want, but you can't key by ID and stay in flat attributes without inventing `warning_<warnid>_level`. A list is the right shape for it." | [comment](https://github.com/home-assistant/core/pull/178358#issuecomment-5206894731) |
| 2026-08-06 | @tklecka | "Actions don't touch the recorder, so the 16 kB attribute ceiling doesn't apply - include everything: `description`, `impacts`, `instructions`, `meteo_text`, `update_reason`" | [comment](https://github.com/home-assistant/core/pull/178358#issuecomment-5207390421) |
| 2026-08-06 | @tklecka | "Free text stays in the action." | [comment](https://github.com/home-assistant/core/pull/178358#issuecomment-5207390421) |

The reviewer's only appearance on #164481 is the 2026-03-03 review. The
three questions above received no reply before the PR closed on 2026-06-08.

On #178358 the code owner reached the attribute critique unprompted, in the
RFC's own terms: positional slots re-point silently when one warning expires
(requirement 2), a concurrent set needs a list keyed by id (requirement 4),
and the 16 KB ceiling is named as the reason the bodies cannot stay in
attributes (§1.1). The resolution chosen was still count-plus-action, with
the attribute set fixed at `type`, `start`, `end`, `warning_id` for the one
top-ranked warning so that "anyone can correlate with the action response".
No dashboard question was asked on this thread; the owner's stated card
concern was per-warning detail "for the top alert without any card changes".

NINA on core `dev` (fetched 2026-10-02), per field:

| Field | Sensor | `nina.get_details` | Attribute |
| :-- | :-- | :-- | :-- |
| headline | yes | yes | deprecated, remove in 2026.11 |
| sender | yes | yes | deprecated, remove in 2026.11 |
| severity | yes | yes | deprecated, remove in 2026.11 |
| affected_areas | yes, `affected_areas_short` | yes, full | deprecated, remove in 2026.11 |
| more_info_url | yes | yes | deprecated, remove in 2026.11 |
| sent | yes, disabled by default | yes | deprecated, remove in 2026.11 |
| start | yes, disabled by default | yes | deprecated, remove in 2026.11 |
| expires | yes, disabled by default | yes | deprecated, remove in 2026.11 |
| description | no | yes | deprecated, remove in 2026.11 |
| recommended_actions | no | yes | deprecated, remove in 2026.11 |
| id | no | yes | kept |

```python
# homeassistant/components/nina/binary_sensor.py, core dev, 2026-10-02
        return {
            ATTR_HEADLINE: data.headline,  # Deprecated, remove in 2026.11
            ATTR_DESCRIPTION: data.description,  # Deprecated, remove in 2026.11
            …
            # Deprecated, remove in 2026.11
            ATTR_RECOMMENDED_ACTIONS: data.recommended_actions,
            ATTR_AFFECTED_AREAS: data.affected_areas,  # Deprecated, remove in 2026.11
            …
        }

    def get_details(self) -> dict[str, str] | None:
        """Return the details of the warning."""
```

```sh
timeout 30 gh api repos/home-assistant/core/pulls/164481
timeout 30 gh api repos/home-assistant/core/pulls/164481/reviews --paginate
timeout 30 gh api repos/home-assistant/core/issues/164481/comments --paginate
timeout 30 gh api repos/home-assistant/core/pulls/164481/comments --paginate
timeout 30 gh api repos/home-assistant/core/pulls/172393
timeout 30 gh api repos/home-assistant/core/pulls/161882
timeout 30 gh api repos/home-assistant/core/pulls/166125
timeout 30 gh api graphql -f query='query { repository(owner:"home-assistant", name:"feature-requests") { discussion(number:3130) { comments(first:50) { nodes { url body replies(first:20) { nodes { author { login } createdAt url body } } } } } } }'
for f in sensor.py services.yaml binary_sensor.py const.py; do timeout 30 gh api "repos/home-assistant/core/contents/homeassistant/components/nina/$f?ref=dev" --jq .content | base64 -d; done
timeout 30 gh api repos/home-assistant/core/pulls/178358
timeout 30 gh api repos/home-assistant/core/issues/178358/comments --paginate
timeout 30 gh api repos/home-assistant/core/pulls/178474
timeout 30 gh api repos/home-assistant/core/issues/178474/comments --paginate
timeout 30 gh api -H 'Accept: application/vnd.github.raw' "repos/home-assistant/core/contents/homeassistant/components/geosphere_austria_warnings/warnings.py?ref=dev"
timeout 30 gh api repos/home-assistant/core/compare/dev...a7rk:geosphere-austria-warnings-add-service --jq '.files[].filename'
timeout 30 gh api -H 'Accept: application/vnd.github.raw' "repos/a7rk/core/contents/homeassistant/components/geosphere_austria_warnings/services.py?ref=geosphere-austria-warnings-add-service"
```

## Reading

Three integrations, six PRs, one resolution. The ECCC attribute-list PR was
blocked by a single review and closed once an action shipped. NINA deprecated
every alert attribute and put the full record behind `nina.get_details`, with
the two long-form fields reachable nowhere else. GeoSphere is the third case
and the first where the code owner wrote the attribute-side critique
themselves, said plainly that no guideline requires it, and chose the action
anyway. The dashboard question was asked three times on #164481 and not
answered. That is the gap §1.6 names: the convention exists in review practice
and has no written form a requirement can be raised against.

GeoSphere also measures what the pattern delivers in a release. HA 2026.10
ships the sensors from #178474 and no action: `warning_sensor_attributes` on
core `dev` carries one warning per bucket and a docstring saying "The full
warning payload is intentionally not exposed on sensor entities." The action
that was to carry the bodies exists only on the author's fork. A municipality
under an orange thunderstorm warning and a yellow rain warning at once exposes
one of them to the state machine, which is the limit reported against the
companion card as weather_alerts_card#342.

## Caveats

- Org-level discussions resolve to the `home-assistant/feature-requests`
  repository in GraphQL. The public URL stays `orgs/home-assistant/...`.
- The NINA field table reads core `dev` on 2026-10-02. The installed HA
  2026.9.3 in `.venv` already ships `nina.get_details`, but the attribute
  removal is a comment on `dev`, not a shipped release.
- #161882's body first proposed three actions (`get_description`,
  `get_recommended_actions`, `get_affected_areas`). One `get_details` shipped.
- Quotes are trimmed to the sentence that carries the point. Follow the
  permalinks for the surrounding paragraph.
- The GeoSphere action is a fork branch, not a PR. Its response shape
  (`active_warnings` and `advance_warnings` lists with `warning_id`,
  `change_id`, `course_id`, `type`, `level`, `start`, `end`, `text`,
  `impacts`, `recommendations`, `meteo_text`, `update_reason`) was read from
  that branch on 2026-10-11 and may change before anything is filed.
- The GeoSphere thread never raised the dashboard question. It is evidence
  that the convention is applied by code owners without a rule, not evidence
  of the question going unanswered; that remains #164481.
