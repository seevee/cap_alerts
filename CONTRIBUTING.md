# Contributing

## Development Setup

```bash
pip install -r requirements_test.txt
pytest                                  # run all tests
ruff check custom_components tests      # lint
mypy custom_components/cap_alerts       # type-check
```

Changing the config flow? Deploy to a running Home Assistant and walk it:

```bash
scripts/flow_walk.py                 # every menu, form, and options schema
scripts/flow_walk.py --skip-network  # omit the MeteoAlarm and WMO fetches
```

It drives the real dialog over the REST API and asserts each step's menu
options and form fields. Read-only: it never submits a step that would create
or update an entry, aborts every flow it opens, and checks the entry count is
unchanged. The tests cover the handler in-process, this covers it under HA's
loader.

See the [Development section of the README](README.md#development) and [docs/architecture.md](docs/architecture.md) for how the integration is structured.

## Workflow

1. Create a feature branch from `main`
2. Make changes; keep `pytest`, `ruff`, and `mypy` green
3. Open a PR targeting `main`

## Translations

User-facing strings live in `custom_components/cap_alerts/strings.json`, with the
English copy mirrored in `translations/en.json`. A PR that adds or renames a
user-facing string must update **both** — that pair is enforced by
`tests/test_translation_keys_in_sync.py` and will fail CI.

Other locales are best-effort and **never block a PR**. Home Assistant falls back
to English for any key a translation omits, so a lagging locale is cosmetic. When
a locale falls behind, the test suite emits a `TranslationDriftWarning` listing
the exact missing keys rather than failing:

```bash
pytest tests/test_translation_keys_in_sync.py   # missing keys appear in the warnings summary
```

Two things *are* enforced for every locale: the file must be valid JSON, and it
must not contain keys absent from `strings.json` (a stale key left behind by a
rename never renders, so it is dead weight).

New translations are welcome as standalone PRs — copy `translations/en.json` to
`translations/<code>.json` and translate the values, leaving the keys untouched.
Use the Home Assistant locale code (`zh-Hans`, `pt-BR`, `nb`, …). Existing locales
are listed in `.github/CODEOWNERS`; add yourself there so you are asked to review
future edits to your file.

## Commit Messages

Use conventional-style prefixes: `feat(scope):`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.

## Changelog

`CHANGELOG.md` is **generated**, not hand-edited — do not edit it directly. It is
produced from the conventional commit history by [git-cliff](https://git-cliff.org/)
(config in `cliff.toml`, pinned in `requirements_test.txt`). Regenerate the whole
file after tagging a release:

```bash
git cliff --config cliff.toml --output CHANGELOG.md
```

Because entries come straight from commit subjects, a clean `type(scope): description`
subject is what lands in the changelog. `chore(release):` and bare `ci:` commits are
skipped. GitHub Release notes for a single tag can be generated with
`git cliff --latest --strip header`.

## AI-Assisted Contributions

AI coding assistants are welcome as tools, under two rules:

- **You own what you submit.** Review and understand AI-assisted code before opening a PR — you are fully responsible for it, including bugs, security issues, and licensing.
- **Disclose the tool — as a tool, not an author.** Note the assistant in a commit trailer using the Linux-kernel convention, e.g. `Assisted-by: Claude:claude-fable-5`. Do not credit an AI via `Co-authored-by:`; that trailer is reserved for human co-authors.
