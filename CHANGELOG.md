# Changelog

## 0.5.1 (2026-09-18)

### Added
- Keep the last good geometry when a fetch fails (#204) (closes #203)

### Fixed
- Prefer the flood footprint over the country outline (#201) (closes #195)
- Give geometry fetches their own timeout and a poll deadline (#202) (closes #196)
- Budget the polygon store per entry and log evictions (#205) (closes #197)

### Documentation
- Restructure for HACS users, move dev material to CONTRIBUTING (#200)

## 0.5.0 (2026-09-14)

### Added
- Prefill GPS fields from the HA home location (#128) (#159)
- Validate NWS GPS mode against /points/ at setup (#161)
- Add a back option to every menu (#140) (#162)
- Raise a repair for ECCC entries that lose NAAD coverage at the sunset (#163) (#165)
- Recover alerts missed during socket downtime from the NAAD repository (#164) (#167)
- Add a device-tracker mode to GDACS (#171) (#173)
- Revalidate the NAAD GeoRSS feed with conditional GET (#182)
- Add superseded_by to incident_removed for ECCC transitions (#191)

### Fixed
- Prefer an English <info> block as the alternate (#154) (#168)
- Match GPS mode on CAM threat polygons, not legacy zones (#176)
- Publish the CAM threat polygon as the alert geometry (#178) (#180)
- Follow CAP identifier lineage to dedupe reissued endings (#188)

### Documentation
- Record the payload bound and the #151 outcome (#169)

### Internal
- Add verify.sh, one command for every CI gate (#166)
- Add a feed vocabulary probe and its scheduled workflow (#174)
- Track only catalog-backed vocabulary in the feed probe (#179)
- Name the alerts behind each feed vocabulary drift (#184) (#186)
- Probe feed vocabulary daily, read NWS history, dedupe comments (#187)
- Bump test pins to HA 2026.9.0, ruff 0.16.6, mypy 2.3.1 (#193)
- Accept MeteoAlarm resource digest/size vocabulary (#198) (closes #194)

## 0.4.0 (2026-08-21)

### Added
- Break the alert count down by active vs upcoming (#104) (closes #99)
- Merge FMI warnings split at the window edge (#107) (closes #98)
- Say why an alert was removed, not just that it was (#113)
- Add the first non-weather CAP provider (#124)
- Add a config-entry diagnostics download (#143) (closes #134)
- One entry per scope, and validate it before creating (#144) (closes #130) (closes #131)

### Fixed
- Report an early ECCC ending as cancel, not expired (#103)
- Drop the scheme name from the region-code example (#105)
- Stop prereleases from rewriting the changelog (#111)
- Collapse re-issues of non-VTEC products into one entity (#115)
- Stop treating one missed poll as a cancellation (#121)
- Require an exit before retaining an expiry-less alert (#122)
- Fire incident_removed once per ending (#145) (#146) (closes #145)
- Bound the attribute payload the recorder stores (#150) (#153)
- Publish versioned geocode schemes under a canonical key (#156)

### Changed
- Move the MeteoFrance dialect into the table (#106)
- Give the GPS helpers one home (#125)
- Split the flow steps into a flows package (#139) (closes #135. The 1,642-line `config_flow.py` becomes a 182-line handler
plus one module per provider under `flows/`, with a `common.py` for what
two or more of them share. Pure move: no renamed steps, no behavior
change, and `strings.json` and `translations/` are untouched.

Not the `config_flow/` package the issue drew. Hassfest checks
`(integration.path / "config_flow.py").is_file()` and fails with "Config
flows need to be defined in the file config_flow.py", so a package of
that name can never validate. The steps go in a sibling package instead
and `config_flow.py` stays a file. Verified against the hassfest
container directly, not just reasoned about: 1 integration, 0 invalid.

Home Assistant registers one flow class per domain, so the split is by
mixin. Each module in `flows/` exports a mixin subclassing `ConfigFlow`
*without* a `domain=`, which is what keeps it out of HANDLERS, and
`config_flow.py` composes them into the one handler that claims the
domain. Step methods keep their names and bodies verbatim, so
`async_step_nws_zone` is still `async_step_nws_zone`, just in
`flows/nws.py` now.

Per-provider option fields move out as `options_schema(entry)` functions
behind an `_OPTIONS_SCHEMAS` dispatch dict. NWS is absent from it, which
is the same fall-through the old elif chain gave it. The save-side
handling in `async_step_init` stays put, since it reads whatever the
composed schema produced.

Verified as a move, not a rewrite. Old file and new modules agree on the
set of `async_step_*` names, `step_id=` literals, `menu_options` lists,
and error keys. The rendered options form was dumped per provider and
the key order is unchanged: shared fields, then the provider block, then
`exclude_marine`, then `geocode_prefixes`. 1051 tests pass, coverage
96.26% total, and the flow modules report 100%.

Tests that patched module globals had to be repointed, because patching
`config_flow` no longer reaches a step module's globals:
`fetch_wmo_sources` to `flows.wmo`, `fetch_regions_for_country` and
`_country_selector` to `flows.meteoalarm`, `_GPS_RE` to `flows.common`.
Same for the private-name imports.

Two tests read the flow's source off disk, the
`async_update_reload_and_abort` guard and the step-id scan. Both read
`config_flow.py` plus `flows/*.py` now, each with an added assert so a
rename fails loudly instead of scanning nothing and passing.

CI's config-flow coverage gate takes both paths in its `--include`, and
the file-structure trees, the add-a-provider steps, and the
`config_flow.py` references in docs/ follow the new layout.)

### Documentation
- Reconcile the docs with the shipped tree (#118)
- Land the removal contract, the CORS bound, and the census (#119)
- Correct four claims the review falsified (#120)
- Separate the abstraction from its binding (#123)
- Re-check every shipped claim against the tree (#149) (closes #133)

### Internal
- Restore the GA-only headings (#112)
- Measure test coverage and gate on it (#136)
- Retire stub mode and import the integration once (#138) (closes #137. Every test file now imports through
`custom_components.cap_alerts.*`. Nothing loads a module a second time by
path, and nothing fabricates `homeassistant.*` or `cap_alerts.*` entries
in sys.modules. Net -1278 lines across 29 files, no test behavior)
- Add a live config flow walk (#141)
- Probe the NAAD stream against the GeoRSS index (#148)
- Sweep long-form text and payload sizes across providers (#152)

## 0.3.1 (2026-08-05)

### Fixed
- Classify MeteoAlarm on the awareness_type code (#101)

## 0.3.0 (2026-08-05)

### Added
- Add Simplified Chinese (zh-Hans) (#58)
- Merge MeteoFrance forecast days into one episode (#70)
- Select the CAP <info> block by language (#72)
- Filter alerts by area-code prefix (#74) (closes #73)
- Parse <circle> geometry and publish point locations (#84) (closes #27)

### Fixed
- Keep pip output out of generated release notes (#62)
- Drop MeteoFrance green no-warning markers (#68)
- Make high-volume sources fit inside the poll timeout (#75)
- Let the update listener own reload decisions (#77)
- Offer every region code of a multi-geocode area (#78) (closes #48)
- Stop offering each region once per feed language (#80)
- Close linear rings before emitting GeoJSON (#86) (closes #85)
- Reach Norwegian info blocks from HA's nb/nn locales (#90) (closes #79)
- Classify multilingual alerts on their English block (#93)

### Changed
- Harden language matching for bare primary subtags (#60)
- Derive geocode aliases from a raw-keyed container (#24) (#64)
- Centralize provider-specific rules in a table (#83)
- Share one CAP polygon parser (#87)
- Share the coordinate-ring builder with GeoRSS (#89)

### Documentation
- Meet the action-response argument in the incident RFC (#67)

### Internal
- Add locale parity guard and contributor policy (#63)
- Add MeteoAlarm France episode-merge sampler (#65)
- Add a live geometry conformance check (#92)

## 0.2.0 (2026-07-30)

### Added
- Surface CLC area geocode as geocode_clc (#19)
- Generalize device-tracker location to all providers (#20)
- Add opt-in exclude-marine-alerts option (#18) (#23)
- Resolve region schemes into a typed geocode container (#29) (closes #25)
- Stream NAAD alerts in real time, GeoRSS as backfill (#49)

### Fixed
- Reconcile prerelease version bump with commit history (#31)
- Activate venv before running tests and lint (#32)
- Use git-cliff CLI binary instead of python -m git_cliff (#33)
- Use git-cliff CLI binary and add release workflow
- Use --tag flag when regenerating changelog in release workflow
- Migrate feed to alertready.ca and rework province filter (#38) (#39)
- Stabilize MeteoFrance alert entity ids (#37) (#41)
- Pre-filter province mode by polygon bbox (#43)
- Guard against truncated NAAD feed downloads (#46)
- Honor ended area groups so alerts don't linger until expiry (#45) (#51)
- Union both NAAD hosts so no live alert is missed (#38) (#52)

### Documentation
- Add CONTRIBUTING.md with AI-assisted contribution policy (#21)
- Extend incident RFC with non-weather grounding and ECCC field findings (#57)

### Internal
- Bump actions/checkout from 6 to 7 in the actions group (#17)
- Add generated changelog and generalize agent guidance (#22)
- Regenerate changelog for v0.2.0-alpha.3
- Remove GitHub Actions publish workflow, add venv activation to publish.sh (#35)
- Bump actions/setup-python from 6 to 7 in the actions group (#50)
- Pre-0.2.0 docs, test pins, and HACS floor (#54)
- Refresh GitHub templates, dependabot pip coverage, agent docs (#55)

## 0.1.1 (2026-06-27)

### Fixed
- Check all polygons for gps coordinates (#15)

## 0.1.0 (2026-05-31)

### Added
- Initial CAP alerts integration
- Provider-aware severity, centralized lifecycle filtering, bilingual model
- Descriptive alert entity names and severity-based state
- Align alert entities with IncidentEntity RFC v1.0
- Externalize GeoJSON via geometry_ref + REST/WS endpoints
- Add warning-triangle + bell icon assets
- Align events and schema with incident RFC (#2)
- Add EUMETNET MeteoAlarm provider (#3)
- Decouple device.name from entry.title (#6)
- Fetch CAP XML body for full description, timestamps, and lifecycle (#7)
- WMO Severe Weather (SWIC) provider (#10)
- Add Singapore, Timor-Leste, and SE-Asia alt-language SWIC feeds (#11)

### Fixed
- Provide last_update_success_time for HA versions that lack it
- Register hydrated alert entities with the platform
- Point setup-python pip cache at requirements_test.txt
- Bump Python to 3.14 for HA 2026.4.x
- Declare http dependency
- Sort keys per hassfest convention
- Strip trailing separator from colour-warning event names (#8)
- Extract identifier string from reference objects (#9)
- Namespace geometry_ref by config entry (#14)

### Changed
- Move polygon cache to in-memory LRU (#1)

### Documentation
- Add README, architecture, and roadmap
- RFC for the incident integration domain (#4)
- Update RFC (#5)
- Rfc revision (#12)
- Revise incident RFC and fill in manifest repo links (#13)

### Internal
- Add pycache to gitignore, fix options flow deprecation
- Unlock test/typecheck jobs and clean lint/type debt
