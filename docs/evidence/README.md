# Evidence behind the incident RFC

One page per finding. The heading states the finding, the table under it says
what was measured and when, and the body is the artifact: a results table, a
copied excerpt, a transcript. Prose is a caption. [`rfc.md`](../../rfc.md)
states the contract and links here.

The last column is the one to read first. **script** means the figure
re-derives from a command in the repo. **fixture** means a captured document
under `tests/fixtures/` shows it. **test** means a test in `tests/` pins the
behavior. **data file** means a committed sample does. **one-time capture**
means the number was measured once and cannot be re-derived from this repo;
it is reported with its date and nothing more. A page can mix kinds, and the
table at its top says which figure is which.

| RFC | Finding | Observed | Reproduces from |
| :-- | :-- | :-- | :-- |
| §1.1 | [The recorder keeps the state and drops every attribute on overflow](the-recorder-keeps-the-state-and-drops-the-attributes.md) | 2026-10-03 (first checked against 2026.7.3 on 2026-08-07) | script |
| §1.2 | [Identity is a per-sender property, not a per-provider one](identity-is-a-per-sender-property.md) | 2026-08-06 (NWS, FMI), 2026-08-08 (GDACS), 2026-09-19 to 09-24 (BBK, AU) | fixture, one-time capture |
| §1.6, §8.1 | [Core review moved alert bodies out of attributes and into actions](core-review-moved-alert-bodies-into-actions.md) | 2026-10-02 (threads fetched with `gh api`, thread dates as GitHub records them) | script |
| §1.6 | [The frontend has no way to read an action result](the-frontend-has-no-way-to-read-an-action-result.md) | 2026-10-02 (discussions fetched with `gh api graphql`, file read from `.venv`) | script |
| §2.2, §6.3 | [The Australian feeds stamp expires as a regeneration TTL, not an end time](australian-feeds-publish-no-end-time.md) | 2026-09-19 (NSW, QLD, WA), 2026-09-20 (TAS), 2026-09-21 (TAS two products) | fixture |
| §2.2 | [ECCC ends an alert in a CAP parameter while msgType stays Update](eccc-ends-alerts-outside-msgtype.md) | 2026-07-22 | fixture, data file, one-time capture |
| §2.2 | [GDACS publishes no end time, so an event ends when the feed withdraws it](gdacs-ends-by-withdrawal.md) | 2026-10-03 (live table). 2026-08-08 (315-item sample, retention figures) | script, fixture |
| §2.3, §7.3 | [An ending fires one incident_removed however long the feed republishes it](one-ending-one-removal.md) | 2026-08-13 (#145), 2026-09-02 (#185) | test |
| §2.3 | [superseded_by points at a successor that never reached NAAD two times in three](superseded-by-dangles-two-times-in-three.md) | 2026-09-02 | test, one-time capture |
| §2.4 | [One GDACS entry held 87% of a global geometry budget and evicted a sibling entry's polygons](geometry-budget-must-be-per-entry.md) | 2026-09-06 | script, test |
| §2.4, §6.2, §6.6 | [NWS zone geometry is heavy-tailed, with a 1,700x span inside one population](zone-geometry-is-heavy-tailed.md) | 2026-08 (one-time census, day not recorded) | script, one-time capture |
| §2.5 | [NWS publishes cancellations where the active endpoint cannot see them](nws-cancellations-never-reach-the-active-endpoint.md) | 2026-08-08 or shortly before (reported in [#121](https://github.com/seevee/cap_alerts/pull/121)), live check 2026-10-03 | script |
| §2.5, §2.3 | [A restart re-validates known alerts instead of re-announcing them](restart-revalidates-instead-of-reannouncing.md) | 2026-10-01 (#249, #250, #252), 2026-10-02 (#257) | test |
| §2.5 | [Retaining an absent alert is only safe when something else can end it](retention-needs-an-exit.md) | 2026-08-08 or shortly before (#122), live check 2026-10-03 | script |
| §2.5 | [The NAAD streaming socket does not share the GeoRSS index gap](the-streaming-socket-does-not-share-the-gap.md) | 2026-08-21 | script, data file, one-time capture |
| §2.5, §1.4 requirement 8 | [Two sanctioned NAAD hosts disagree on which alerts are live](two-naad-hosts-disagree-on-live-alerts.md) | 2026-07-22 | script, data file |
| §2.7 | [The alternate language block is chosen by rule, not by document order](the-alternate-language-is-a-rule-not-document-order.md) | 2026-08-16 (report), 2026-08-21 (sweep) | script, test |
| §3.3, §1.4 requirement 4 | [MeteoAlarm shows one alert when there are several](meteoalarm-shows-one-alert-when-there-are-several.md) | 2026-10-02 (issues fetched with `gh api`, topics fetched as `.json`) | script |
| §3.3, §8.1 | [One sensor per alert was proposed in core in 2020](one-sensor-per-alert-was-proposed-in-2020.md) | 2026-10-02 (threads fetched with `gh api`, thread dates as GitHub records them) | script |
| §3.8 | [Two of fourteen CAP endpoints send Access-Control-Allow-Origin](cors-two-of-fourteen-endpoints.md) | 2026-08-08 (first seven rows), 2026-10-03 (GDACS, BBK and Australian rows) | script |
| §4.1 | [NAAD and BBK carry non-weather hazards through the weather code path](naad-carries-non-weather-hazards-through-one-code-path.md) | 2026-07-22 (NAAD), 2026-09-04 (BBK fixture) | fixture, data file, one-time capture |
| §5, §2.1 | [A second card read the raw severity instead of the normalized one](a-second-card-read-the-raw-severity.md) | 2026-10-03 (the dev-box check reported in the second comment, UTC) | script |
| §5 | [The ECCC provider reduces a national feed to single-digit entities](the-provider-layer-hands-core-single-digits.md) | 2026-07-23 (figures first committed in `fa44e5a`), 2026-10-03 | script |
| §7.2 | [A 291-area advisory overflowed the payload after the ladder had spent every text field](area-lists-overflow-after-text-is-spent.md) | 2026-09-30 | script, test |
| §7.2, §2.4 | [A per-field text cap shreds alerts that fit and cannot rescue the one that overflows](per-field-text-caps-fail-both-ways.md) | 2026-08-16 | script |
