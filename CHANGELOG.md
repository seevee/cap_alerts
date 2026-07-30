# Changelog

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
