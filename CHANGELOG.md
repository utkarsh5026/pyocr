# Changelog

## Unreleased

- Better progress for extraction jobs: an estimated time left and seconds per page (cache hits don't skew it),
  failed pages shown in red on the progress bar with a "done with errors" badge, and LLM pages still waiting
  for an `LLM_CONCURRENCY` slot listed as "waiting" instead of "working on". `/api/jobs` adds
  `seconds_per_page`, `eta_seconds` and `waiting_pages`. Transcribing a single page in the workspace now shows
  an elapsed-time counter.
- LLM costs at catalog list price, in US dollars and Indian rupees: each job shows its spend so far and, while
  running, a projection for the whole job; each workspace transcription shows its own cost. Hover for the price
  basis. New setting `USD_INR_RATE` (default 92.5, not fetched live). Models without a catalog price show
  "cost unknown". Excludes taxes (e.g. GST) and batch/caching discounts. `/api/jobs` and the page LLM endpoints
  add a `cost` object. The Models page shows list prices in rupees under the dollar prices.
- Workspaces: upload several files or a `.zip` at once and keep them together under one editable name.
  The upload dialog suggests names (from the file names, or `adjective-scientist` style like `festive-albattani`).
  New Workspaces page and workspace page (rename, add files, remove documents), workspace badges and filter in
  the Library, `POST /api/uploads` (zips unpacked, bad files skipped with a reason), `/api/workspaces` endpoints,
  and `pyocr import FILES... [-w NAME]`. Stored in `data/workspaces/`; document folders are unchanged.
- New default models: `OPENAI_MODEL` is now `gpt-6.2-sol` (was `gpt-5`, which OpenAI has deprecated) and
  `GEMINI_MODEL` is now `gemini-3.8-flash` (was `gemini-2.5-flash`, which Google routes to
  `gemini-3.5-flash-lite` from Dec 1, 2026). Set either variable to keep the old model. Results from the old
  defaults stay cached as their own source; new runs with the default model create a new one.

- Set provider credentials and endpoints from the **Providers** page. Values are saved server-side in
  `DATA_DIR/credentials.json` (owner-only permissions), applied immediately without a restart, take precedence
  over environment variables, and can be cleared to fall back to the environment. Secret values are never sent
  back to the browser; cross-site writes are refused.
- New **Models** page: a searchable catalog of vision models (Claude, OpenAI, Gemini, open-weight) rated for
  verbatim transcription, with price, status (current / preview / deprecated) and the evidence behind each
  rating. *List models* asks Anthropic, OpenAI, Gemini or an OpenAI-compatible server which models your key can
  use and rates each one.
- Every model box now shows a rating hint as you type and offers rated suggestions; custom names (fine-tunes,
  deployments, local tags) are still accepted and matched loosely to catalog entries where possible.
- Suggested models refreshed: the OpenAI and Gemini suggestions no longer list deprecated models.
- API: `GET /api/models`, `GET /api/models/lookup`, `GET /api/providers/{id}/models`,
  `GET|POST /api/providers/{id}/credentials`.

- Developer task runner: `make` (or `python makefile.py`) lists grouped, rich-rendered commands for setup,
  linting/tests, running the app (incl. auto-reload `make dev`), building, Docker and housekeeping. `rich` is
  now a dev dependency.
- Subtle UI motion: pages and card grids ease in, cards lift on hover, buttons respond to presses, tab panels
  and results fade in, and toasts fade out. While Tesseract or an LLM is working on a page, a scan line sweeps
  the page image, the button shows a spinner (with a flowing gradient for the model), the text box shimmers,
  and the finished text fades in. Running jobs get a pulsing badge and a moving progress bar. All of it turns
  off when the OS asks for reduced motion.
- Icons throughout the UI: the top nav, document tabs, text sources (native / OCR / LLM), action buttons,
  section headings, status badges, the "How it works" steps and empty states. They follow the light/dark
  theme, and on narrow screens the top nav collapses to icons only.
  A few emojis add colour in decorative spots only (the home page steps and status cards, the upload area,
  empty states and the error page); everything you click keeps the line icons.

## 0.2.0

- Multi-page web app with bookmarkable URLs: Home, Library, document Overview & extract, Page inspector,
  Full text reader, document-level Compare, library-wide Search, Jobs, Providers and an in-app Help guide.
- Background extraction jobs for the whole document or page ranges (Tesseract or any LLM), with progress,
  cancel, per-page errors and token totals. Results are cached per page, so re-runs only do missing work.
- Document-level comparison of any two sources (OCR vs LLM, native vs LLM, model vs model): micro-averaged
  CER/WER, CER per page chart, worst pages, and an inline diff for each page.
- Export formats: TXT, Markdown, JSON and JSONL (one page per line).
- Providers page showing which settings are set, defaulted or missing, plus a **Test connection** button.
- CLI: `pyocr init`, `pyocr doctor`, `pyocr extract` (native / ocr / llm → txt / md / json / jsonl),
  `pyocr serve --open`, `python -m pyocr_toolkit`.
- Packaging: distribution renamed to `pyocr-toolkit` and import package to `pyocr_toolkit` (the `pyocr` name on
  PyPI belongs to an unrelated project). Dockerfile with Tesseract, docker-compose, GitHub Actions CI
  (Linux/macOS/Windows, wheel install smoke test, Docker smoke test).

## 0.1.0

- First version: upload, native text, Tesseract OCR, LLM transcription (Claude, OpenAI, Azure OpenAI,
  Claude on Foundry, Gemini, OpenAI-compatible), per-page comparison and search.
