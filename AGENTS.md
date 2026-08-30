# AGENTS.md

## Scope

Build a local, personal-use OALD10 bilingual data set and serve it through five
read-only EasyDict-compatible endpoints. Do not add authentication, uploads,
synchronization, a web UI, or example audio unless the scope changes.

## Working rules

- Use Python 3.12 and `uv`; run project tools with `uv run`.
- Use Ruff for formatting/linting and Ty for type checking.
- Keep Compose variables explicit; do not add shell-style defaults.
- Make the smallest change that fully solves the task and preserve unrelated work.

## Data boundaries

- Never commit or distribute `source-dictionary/`, `sources/`, `generated/`,
  `.env`, SQLite databases, or extracted Oxford media.
- HTTP handlers may only read generated SQLite/JSON files; they must never open
  MDX/MDD files or trigger a build.
- Definitions and examples must remain paired English/Simplified Chinese.
- Preserve examples under their source sense, including example-only and
  reference-only senses.
- Remove example HTML/highlighting and all `audio/example` references.

## Verification

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check src tests
uv run pytest
docker compose config --quiet
```

The full local-source acceptance check is `uv run oald10 build`; its audit must
match the fixed counts in `README.md`.
