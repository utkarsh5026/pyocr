# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Setup

```bash
git clone https://github.com/utkarsh5026/pyocr && cd pyocr
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sudo apt-get install tesseract-ocr   # optional; OCR tests are skipped without it
pytest && ruff check src tests
pyocr serve
```

No API keys are needed for the test suite: provider code is exercised against mocked HTTP, and the API tests use a
fake provider. To try the UI end to end without keys, point the OpenAI-compatible provider at any local server
(for example Ollama with a vision model).

## Code layout

```
src/pyocr_toolkit/
  config.py        settings (env vars / .env)
  documents.py     storage + everything PyMuPDF: rendering, native text, Tesseract OCR, caches
  service.py       shared operations: run an LLM on a page, list text sources, page text by source
  jobs.py          background extraction jobs (whole document / page ranges)
  compare.py       CER / WER and word-level diffs
  search.py        word-box and snippet search
  api.py           JSON API under /api + the app factory
  web.py           server-rendered HTML pages (Jinja2 templates in templates/)
  cli.py           `pyocr` command
  llm/             one module per provider family, plus prompts.py
  static/          CSS + one small vanilla-JS file per page (no build step)
```

## Adding an LLM provider

1. Subclass `pyocr_toolkit.llm.base.Provider` and set `id`, `label`, `env_vars`, `suggested_models` and `docs_url`.
2. Implement `default_model`, `missing_settings()` and `async _transcribe(req, model)`, returning a
   `TranscriptionResult`. Raise `ProviderError` with a readable message on failure, and add a warning when
   output was truncated.
3. Add its settings to `config.py` and to **both** `.env.example` and `src/pyocr_toolkit/env.example` (a test
   checks they match).
4. Register the class in `llm/__init__.py`.
5. Add a mocked-HTTP test in `tests/test_providers.py`.

## Style

- `ruff check` must pass (configured in `pyproject.toml`).
- Keep the frontend dependency-free: plain HTML/CSS/JS served as static files.
- User-facing errors should say what to do next (which setting to set, which package to install).
