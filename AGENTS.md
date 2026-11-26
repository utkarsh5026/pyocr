# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, Copilot, ...) working in this repo.
Human contributors: see [CONTRIBUTING.md](CONTRIBUTING.md). Both describe the same rules.

## What this project is

`pyocr-toolkit` is a local FastAPI web app + CLI for checking PDF/image text extraction. Every page has three
text **sources**: native text (PyMuPDF), Tesseract OCR, and LLM transcription (Claude, OpenAI, Azure OpenAI,
Claude on Foundry, Gemini, any OpenAI-compatible server). Users compare sources (CER/WER, word diffs), search,
and export text.

- Distribution name: `pyocr-toolkit`. Import package: `pyocr_toolkit`. CLI: `pyocr`. Never rename to plain
  `pyocr` (that PyPI name is an unrelated library).
- Python 3.10+ (CI runs 3.10–3.13 on Linux, plus macOS and Windows on 3.12). Don't use 3.11+ only syntax.

## Commands

```bash
pip install -e ".[dev]"            # setup
ruff check src tests               # lint (must pass; config in pyproject.toml, line length 130)
ruff check --fix src tests         # autofix import order etc.
pytest -q                          # all tests; no API keys or network needed
pytest tests/test_providers.py -q  # one file
pytest -k compare -q               # by name
pyocr serve --open                 # run the app on http://127.0.0.1:8000
pyocr doctor                       # check Tesseract / providers / data dir
make                               # grouped list of dev tasks (aliases for `python makefile.py <task>`)
```

Done means: `ruff check src tests` and `pytest -q` both pass. OCR tests skip automatically when `tesseract`
isn't on PATH (`HAS_TESSERACT` in `tests/conftest.py`); say so if they were skipped.

## Layout

```text
src/pyocr_toolkit/
  config.py      Settings (pydantic-settings; env vars / .env)
  documents.py   DocumentStore + OpenDocument: storage, PyMuPDF rendering, native text, Tesseract OCR, caches
  service.py     Services: run an LLM on a page, list sources, page text by source; parse_pages()
  jobs.py        JobManager: in-memory background extraction jobs over page lists
  compare.py     normalize / score (CER, WER) / word_diff
  search.py      word-box and snippet search
  api.py         JSON API under /api + create_app() (the app factory)
  web.py         server-rendered HTML pages (Jinja2, templates/)
  cli.py         `pyocr` argparse CLI
  llm/           base.py (Provider ABC), one module per provider family, prompts.py, catalog.py (rated models), __init__.py registry
  credentials.py provider settings saved from the UI (DATA_DIR/credentials.json)
  templates/     Jinja2; base.html holds the nav; partials start with _
  static/        styles.css + one vanilla-JS file per page; common.js shared helpers
tests/           conftest.py builds sample PDF/PNG in memory; test_api.py has FakeProvider
Makefile         thin aliases: every target runs `python makefile.py <target>`
makefile.py      rich-rendered task runner; add a task there AND its target in Makefile (test_makefile.py checks)
docs/            static GitHub Pages site (index.html, style.css, screenshots/), deployed by .github/workflows/pages.yml
```

If a UI change makes the screenshots in `docs/screenshots/` (used by the README and the site) clearly outdated,
say so; regenerate them only when asked.

Data flow: `api.py`/`web.py`/`cli.py` → `Services` / `JobManager` → `DocumentStore` / `Provider`.
Shared logic goes in `service.py`, not duplicated across API, web and CLI.

## Invariants (don't break these)

- **Page numbers are 0-based** in Python and in `/api`. The UI and the CLI `--pages` (`"1-3,7"`, via
  `parse_pages`) are 1-based. Convert at the edge only.
- **Source ids**: `native`, `ocr`, `llm:<key>`, and `llm` (= latest LLM result per page). The LLM key comes from
  `OpenDocument.llm_cache_key(provider_id, model, system_prompt)`, so it is the same on every page. Don't change
  how keys are derived without a migration story: it orphans every user's cached results.
- **On-disk layout** (`data/documents/<32-hex id>/meta.json`, `source.<ext>`, `ocr/<page>.json`,
  `llm/<page>/<key>.json`, plus `data/credentials.json` for settings saved on the Providers page) is user data.
  Changes must stay readable for existing folders.
- **PyMuPDF is not thread-safe.** Every access to an open `pymupdf.Document` goes through the per-document lock
  in `documents.py`. FastAPI runs sync endpoints in a thread pool, so this matters. In async code, render with
  `asyncio.to_thread`.
- **Secrets never reach the browser.** `env_status()` reports set / default / missing, never values; the
  credentials API (`credentials.py`) adds values only for non-secret settings (no `KEY`/`SECRET`/`TOKEN` in the
  name). Writes to it go through `_require_same_origin`. Tests: `test_providers_page_never_shows_values`,
  `test_credentials_saved_from_ui`.
- **The model catalog** (`llm/catalog.py`) suggests and rates; it never restricts. Any typed model name must keep
  working. Update ratings with a source and bump `CATALOG_UPDATED`.
- **LLM calls** go through `Services.run_llm_page`, which caches results and respects `llm_semaphore`
  (`LLM_CONCURRENCY`). Don't call `provider.transcribe` directly from endpoints.
- **The verbatim prompt** in `llm/prompts.py` is the product's yardstick ("transcribe, don't correct"). Changing
  its text changes every cache key; treat it as a deliberate, changelog-worthy change.
- **Errors**: raise `DocumentError` (→ 400/404, `OCRUnavailableError` → 503) or `ProviderError` (→ 502). Messages
  are shown to users and should say what to do next (which env var to set, what to install).
- **Jobs are in memory.** Results are written to per-page caches as each page finishes, so a restart loses the job
  list, not the results. Keep it that way.

## Conventions

- Every module starts with `from __future__ import annotations` and a docstring explaining *why*, not just what.
- Type hints on public functions; `dataclass` for plain records, pydantic `BaseModel` for request bodies.
- **Frontend has no build step and no dependencies.** Plain HTML/CSS/JS. Each page template loads `common.js`
  then its own `static/<page>.js`. Use the helpers in `common.js` (`$`, `$$`, `api`, `postJSON`, `toast`, `guard`,
  `esc`). Always `esc()` text before putting it in `innerHTML`. Support light and dark themes (CSS variables in
  `styles.css`).
- Tests never touch the network. Providers are tested against mocked HTTP; API/web tests use `FakeProvider` and
  `Settings(data_dir=tmp_path, _env_file=None)` so a developer's `.env` can't leak in.
- `.env.example` and `src/pyocr_toolkit/env.example` must be byte-identical (`test_env_template_in_sync`).
- Version lives in `src/pyocr_toolkit/__init__.py` and `pyproject.toml`; keep them in sync and add a
  `CHANGELOG.md` entry for user-visible changes.

## Common tasks

### Add an LLM provider

1. Subclass `llm.base.Provider`; set `id`, `label`, `env_vars`, `suggested_models`, `docs_url`, `model_hint`.
2. Implement `default_model`, `missing_settings()`, `async _transcribe(req, model) -> TranscriptionResult`. Raise
   `ProviderError` with a readable message; append to `warnings` when output was truncated or refused.
3. Add settings to `config.py` and to **both** env templates.
4. Register in `PROVIDER_CLASSES` in `llm/__init__.py`.
5. Add a mocked-HTTP test to `tests/test_providers.py`; update the README provider table.

### Add a web page

1. Route in `web.py` using the local `page(request, template, nav, **ctx)` helper.
2. Template in `templates/` extending `base.html`; add a nav link in `base.html` if it's top-level.
3. Script in `static/<page>.js`, loaded in `{% block scripts %}` with `?v={{ version }}`.
4. Add the URL + a needle string to `test_html_pages_render`, and the asset to its static list.
5. Document it on the Help page (`templates/help.html`) and in the README page table.

### Add an API endpoint

Add it inside `build_api()` in `api.py` under `/api`, with a pydantic body model if it takes JSON. Put real logic
in `Services`. Add a test in `tests/test_api.py` using the `client` and `doc_id` fixtures.

## Safety

- Don't commit `.env`, `data/`, or real documents. Test fixtures are generated in `conftest.py`.
- `pyocr serve` binds to `127.0.0.1` on purpose: there's no auth. Don't change the default host.
- Don't add telemetry or send page images anywhere except the provider the user chose.
