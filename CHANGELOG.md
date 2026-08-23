# Changelog

## 0.4.1 — 2026-08-23

### Added
- Add a device-tracker mode to GDACS (#171) (#173) (a9022f2…)

## 0.4.0 — 2026-08-21

### Added
- Break the alert count down by active vs upcoming (#104) (1f79245…)
- Merge FMI warnings split at the window edge (#107) (92df357…)
- Say why an alert was removed, not just that it was (#113) (cbbd9b4…)
- Add the first non-weather CAP provider (#124) (77345c7…)
- Add a config-entry diagnostics download (#143) (966bd8f…)
- One entry per scope, and validate it before creating (#144) (a5bdc3f…)

### Changed
- Move the MeteoFrance dialect into the table (#106) (9d60554…)
- Give the GPS helpers one home (#125) (922cc57…)
- Split the flow steps into a flows package (#139) (86d7799…)

### Documentation
- Reconcile the docs with the shipped tree (#118) (0f3908c…)
- Land the removal contract, the CORS bound, and the census (#119) (923f92f…)
- Correct four claims the review falsified (#120) (46005ba…)
- Separate the abstraction from its binding (#123) (abc701e…)
- Re-check every shipped claim against the tree (#149) (4760002…)

### Fixed
- Report an early ECCC ending as cancel, not expired (#103) (4a77dcd…)
- Drop the scheme name from the region-code example (#105) (68abe89…)
- Stop prereleases from rewriting the changelog (#111) (05a1bb5…)
- Collapse re-issues of non-VTEC products into one entity (#115) (1e33c2a…)
- Stop treating one missed poll as a cancellation (#121) (b6e2d82…)
- Require an exit before retaining an expiry-less alert (#122) (d68bbb1…)
- Fire incident_removed once per ending (#145) (#146) (ff0a5b2…)
- Bound the attribute payload the recorder stores (#150) (#153) (728354f…)
- Publish versioned geocode schemes under a canonical key (#156) (42e4109…)

### Maintenance
- Restore the GA-only headings (#112) (e555798…)
- Measure test coverage and gate on it (#136) (cd3fb0e…)
- Add a live config flow walk (#141) (150fd89…)
- Probe the NAAD stream against the GeoRSS index (#148) (c1a4992…)
- Sweep long-form text and payload sizes across providers (#152) (1a6751a…)

### test
- Retire stub mode and import the integration once (#138) (66a53d1…)

## 0.3.1 — 2026-08-05

### Fixed
- Classify MeteoAlarm on the awareness_type code (#101) (3974ef1…)

## 0.3.0 — 2026-08-05

### Added
- Add Simplified Chinese (zh-Hans) (#58) (dbb9d6e…)
- Merge MeteoFrance forecast days into one episode (#70) (a4066a6…)
- Select the CAP <info> block by language (#72) (1514db8…)
- Filter alerts by area-code prefix (#74) (7c0379a…)
- Parse <circle> geometry and publish point locations (#84) (fe54692…)

### Changed
- Harden language matching for bare primary subtags (#60) (e248db1…)
- Derive geocode aliases from a raw-keyed container (#24) (#64) (8809a74…)
- Centralize provider-specific rules in a table (#83) (3ea9e97…)
- Share one CAP polygon parser (#87) (94f894c…)
- Share the coordinate-ring builder with GeoRSS (#89) (ae30d6b…)

### Documentation
- Meet the action-response argument in the incident RFC (#67) (f955d4b…)

### Fixed
- Keep pip output out of generated release notes (#62) (79cd02b…)
- Drop MeteoFrance green no-warning markers (#68) (f2de1db…)
- Make high-volume sources fit inside the poll timeout (#75) (5e8c177…)
- Let the update listener own reload decisions (#77) (220ffcc…)
- Offer every region code of a multi-geocode area (#78) (c7eaef4…)
- Stop offering each region once per feed language (#80) (aa1e8f0…)
- Close linear rings before emitting GeoJSON (#86) (d4586a2…)
- Reach Norwegian info blocks from HA's nb/nn locales (#90) (80116c5…)
- Classify multilingual alerts on their English block (#93) (64892f6…)

### Maintenance
- Add locale parity guard and contributor policy (#63) (8446211…)
- Add MeteoAlarm France episode-merge sampler (#65) (25e8402…)
- Add a live geometry conformance check (#92) (0e0c0e8…)

## 0.2.0 — 2026-07-30

### Added
- Surface CLC area geocode as geocode_clc (#19) (abe58f1…)
- Generalize device-tracker location to all providers (#20) (2fc7b88…)
- Add opt-in exclude-marine-alerts option (#18) (#23) (ad81b5e…)
- Resolve region schemes into a typed geocode container (#29) (a872764…)
- Stream NAAD alerts in real time, GeoRSS as backfill (#49) (f019d08…)

### Documentation
- Add CONTRIBUTING.md with AI-assisted contribution policy (#21) (70e0273…)
- Extend incident RFC with non-weather grounding and ECCC field findings (#57) (fa44e5a…)

### Fixed
- Reconcile prerelease version bump with commit history (#31) (33d7fa6…)
- Activate venv before running tests and lint (#32) (f650e9e…)
- Use git-cliff CLI binary instead of python -m git_cliff (#33) (69ca77b…)
- Use git-cliff CLI binary and add release workflow (67436dc…)
- Use --tag flag when regenerating changelog in release workflow (719d475…)
- Migrate feed to alertready.ca and rework province filter (#38) (#39) (3b5f8b6…)
- Stabilize MeteoFrance alert entity ids (#37) (#41) (21fd560…)
- Pre-filter province mode by polygon bbox (#43) (c2560e6…)
- Guard against truncated NAAD feed downloads (#46) (961c012…)
- Honor ended area groups so alerts don't linger until expiry (#45) (#51) (f9ecb7c…)
- Union both NAAD hosts so no live alert is missed (#38) (#52) (3006d56…)

### Maintenance
- Bump actions/checkout from 6 to 7 in the actions group (#17) (71431ac…)
- Add generated changelog and generalize agent guidance (#22) (aad1fd9…)
- Regenerate changelog for v0.2.0-alpha.3 (e1cb081…)
- Remove GitHub Actions publish workflow, add venv activation to publish.sh (#35) (7ff168a…)
- Bump actions/setup-python from 6 to 7 in the actions group (#50) (68d2019…)
- Pre-0.2.0 docs, test pins, and HACS floor (#54) (4102659…)
- Refresh GitHub templates, dependabot pip coverage, agent docs (#55) (c383a57…)

## 0.1.1 — 2026-06-27

### Fixed
- Check all polygons for gps coordinates (#15) (82c3933…)

## 0.1.0 — 2026-05-31

### Added
- Initial CAP alerts integration (c1c557f…)
- Provider-aware severity, centralized lifecycle filtering, bilingual model (da3359e…)
- Descriptive alert entity names and severity-based state (68294f8…)
- Align alert entities with IncidentEntity RFC v1.0 (8abf8ed…)
- Externalize GeoJSON via geometry_ref + REST/WS endpoints (9c0307e…)
- Add warning-triangle + bell icon assets (fb32368…)
- Align events and schema with incident RFC (#2) (099247b…)
- Add EUMETNET MeteoAlarm provider (#3) (cd101d1…)
- Decouple device.name from entry.title (#6) (b22f517…)
- Fetch CAP XML body for full description, timestamps, and lifecycle (#7) (035d866…)
- WMO Severe Weather (SWIC) provider (#10) (c7c3dc0…)
- Add Singapore, Timor-Leste, and SE-Asia alt-language SWIC feeds (#11) (c232e34…)

### Changed
- Move polygon cache to in-memory LRU (#1) (7019177…)

### Documentation
- Add README, architecture, and roadmap (a3c3dde…)
- RFC for the incident integration domain (#4) (76fffd1…)
- Update RFC (#5) (103ad42…)
- Rfc revision (#12) (b260c59…)
- Revise incident RFC and fill in manifest repo links (#13) (dbbda4e…)

### Fixed
- Provide last_update_success_time for HA versions that lack it (297a051…)
- Register hydrated alert entities with the platform (1f96bb3…)
- Point setup-python pip cache at requirements_test.txt (daa23be…)
- Bump Python to 3.14 for HA 2026.4.x (b8b91c5…)
- Declare http dependency (ae12326…)
- Sort keys per hassfest convention (0b05bbb…)
- Strip trailing separator from colour-warning event names (#8) (556c5d4…)
- Extract identifier string from reference objects (#9) (9735d4a…)
- Namespace geometry_ref by config entry (#14) (bb33e57…)

### Maintenance
- Add pycache to gitignore, fix options flow deprecation (086e3f1…)
- Unlock test/typecheck jobs and clean lint/type debt (eeeede8…)
