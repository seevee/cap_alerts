# Contributing

## Development Setup

```bash
pip install -r requirements_test.txt
pytest                                  # run all tests
ruff check custom_components tests      # lint
mypy custom_components/cap_alerts       # type-check
```

See the [Development section of the README](README.md#development) and [docs/architecture.md](docs/architecture.md) for how the integration is structured.

## Workflow

1. Create a feature branch from `main`
2. Make changes; keep `pytest`, `ruff`, and `mypy` green
3. Open a PR targeting `main`

## Commit Messages

Use conventional-style prefixes: `feat(scope):`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.

## AI-Assisted Contributions

AI coding assistants are welcome as tools, under two rules:

- **You own what you submit.** Review and understand AI-assisted code before opening a PR — you are fully responsible for it, including bugs, security issues, and licensing.
- **Disclose the tool — as a tool, not an author.** Note the assistant in a commit trailer using the Linux-kernel convention, e.g. `Assisted-by: Claude:claude-fable-5`. Do not credit an AI via `Co-authored-by:`; that trailer is reserved for human co-authors.
