# CLAUDE.md

@AGENTS.md

## Claude Code specifics

The project rules, commands and invariants are in AGENTS.md (imported above), so every agent tool reads the same
source. Edit AGENTS.md, not this file, when a rule changes.

### Slash commands (`.claude/commands/`)

| Command | What it does |
|---|---|
| `/check` | Lint + tests, fixes what's safe to fix, reports skipped OCR tests |
| `/add-provider <name>` | Adds a new LLM provider end to end (class, settings, env templates, registry, test, docs) |
| `/add-page <name>` | Adds a new server-rendered page (route, template, JS, nav, test, Help) |
| `/smoke` | Starts the app on a spare port and hits every page and key API route |
| `/release <version> [--dry-run] [--testpypi]` | Bumps the version, writes the CHANGELOG entry, runs checks, then (after you confirm) commits, tags and creates the GitHub Release, which publishes to PyPI |

### Subagents (`.claude/agents/`)

- `pyocr-reviewer`: reviews a diff against the invariants in AGENTS.md (0-based pages, cache keys, PyMuPDF
  locking, secrets, frontend rules). Read-only.
- `provider-engineer`: builds or debugs LLM provider integrations and their mocked-HTTP tests.
- `test-writer`: writes pytest tests in this repo's style (no network, generated fixtures, `FakeProvider`).

### Working notes

- Run `ruff check src tests && pytest -q` before saying a change is done.
- When the user asks to see a UI change, run `pyocr serve --port <spare port>` with `DATA_DIR` pointed at the
  scratchpad, not the repo's `./data`.
- Check model defaults (`ANTHROPIC_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`) against current provider docs before
  changing them. Don't guess model ids.
