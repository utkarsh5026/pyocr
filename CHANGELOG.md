# Changelog

## Unreleased

- Faster Overview for large documents: per-page facts (size, text-layer characters, images) are computed once and
  saved to `pages.json` in the document folder, so later visits to a 2,000-page PDF's Overview drop from ~2.5 s
  to ~40 ms. Uploads build `pages.json` in the background right away, so the first visit is fast too, and page
  renders keep working while it runs. The page grid draws thumbnails in batches of 200 as you scroll. Existing
  folders (and documents added with `pyocr add`) get `pages.json` on first view; it is derived data and is rebuilt
  if missing or damaged.

## 0.6.0 - 2026-11-27

- **Structured data extraction**: a new prompt, *Structured data (fields → JSON)*, fills a JSON Schema from each page
  instead of transcribing it. Built-in schemas for invoices, receipts and form fields, or your own JSON Schema
  (checked as you type). Providers enforce the schema natively where they can (Claude structured outputs, OpenAI /
  Azure strict `response_format`, Gemini `response_json_schema`); older Claude models and OpenAI-compatible servers
  get it in the prompt, with a warning. Values are copied as printed, and fields that aren't on the page are `null`.
  The LLM tab shows the result as a field table (fields found, line items, schema problems on their field) with the
  raw JSON one click away. Every reply is parsed and validated against the schema; results store `data`,
  `data_errors`, `fields_filled` and `fields_total` next to the text.
- **Schema editor**: build your own schema field by field (name, type, hint for the model, required; tables and
  groups nest) without writing JSON Schema, or edit it as JSON in an editor with colours, line numbers and the
  error line marked. *Fix syntax* repairs quotes, unquoted keys, missing or trailing commas, unclosed brackets,
  comments, Python `True`/`None` and type names such as `str` or `bool`. Each edit is checked: mistakes come with
  suggestions ("did you mean 'properties'?"), and a note says whether the chosen provider and model enforce the
  schema or only get it in the prompt. An example output, as the field table or as JSON, shows what a result will
  look like before you pay for a run.
- **Draft fields from plain words**: describe what to extract ("invoice number, date, every line item with its
  amount") and the chosen model drafts the fields, optionally looking at the current page. The draft opens in the
  field editor for review, and *Change my current fields* refines it step by step ("also add the due date"). One
  paid call per draft (tokens and cost shown), through `LLM_CONCURRENCY`. API: `POST /api/extract/draft-schema`
  (`description`, `provider`, `model`, optional `current_schema`, `doc_id` and `page`; same-origin only).
- The schema is part of the LLM cache key, so each schema is its own source. Plain transcriptions keep their
  existing keys, so cached results are unaffected.
- API: `prompt_mode: "extract"` with `extract_schema` (a preset id or a JSON Schema) on
  `POST /api/documents/{id}/pages/{n}/llm` and in job bodies, `POST /api/extract/check-schema` (validation notes,
  example output, syntax error position and a repaired version; takes an optional `provider` and `model`), `extract_presets` in
  `/api/config`, and a per-page `data` object in JSON / JSONL exports of an extraction (one source, and
  *Export all* as `data: {source_id: fields}`). CLI:
  `pyocr extract --mode extract --schema invoice|receipt|form|<file.json>`.
- The README and website now install from PyPI (`pipx install pyocr-toolkit`) instead of from GitHub.
- Existing results and cache keys are unchanged.

## 0.5.0 - 2026-11-27

- **More OCR engines.** Besides Tesseract: RapidOCR, EasyOCR, PaddleOCR and docTR (local, optional extras such as
  `pip install "pyocr-toolkit[easyocr]"`), and Google Cloud Vision, Azure AI Document Intelligence, AWS Textract
  and Mistral OCR (cloud, configured with environment variables). Each engine is a separate source
  (`ocr:<engine>`; `ocr` stays Tesseract) that can be searched, compared and exported.
  - The page inspector, Overview extract form, background jobs and the CLI (`--engine ocr:<engine>`,
    `--ocr-engine`, `pyocr engines`) can pick the OCR engine. The Providers page lists every engine with its status
    and a Test button, and cloud OCR keys and engine settings can be saved there like LLM provider credentials.
  - New settings: `GOOGLE_VISION_API_KEY`, `GOOGLE_VISION_LANGUAGE_HINTS`, `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`,
    `AZURE_DOCUMENT_INTELLIGENCE_KEY`, `AWS_REGION` (plus the usual AWS credentials), `MISTRAL_API_KEY`,
    `MISTRAL_OCR_MODEL`, `EASYOCR_LANGUAGES`, `PADDLEOCR_LANGUAGE`, `OCR_GPU`, `OCR_MAX_EDGE_PX`, `OCR_TIMEOUT_S`.
  - Existing Tesseract results (`ocr/<page>.json`) are read as before; other engines cache under `ocr/<engine>/`.
- **Other PDF libraries.** Besides PyMuPDF, the text layer can be read with pypdf, pdfplumber, pdfminer.six and
  pypdfium2 (optional: `pip install "pyocr-toolkit[pdf]"`). Each installed library is its own source
  (`native:<library>`; `native` stays PyMuPDF) that can be compared and exported. The page inspector's Native tab
  has a library picker and a **Test all libraries** button that shows which libraries get text out of the page.
  CLI: `--engine native:<library>` and `pyocr libraries`; API: `?library=` on `/text/native`, a new
  `/pages/{page}/text/native/libraries` endpoint, and `text_libraries` in `/api/config`. Results are cached under
  `native/<library>/<page>.json` and re-read when the library version changes.
- **Export all sources.** Download everything extracted for a document in one file: Overview → *Export all*,
  the Full text *Download* menu, or `GET /api/documents/{id}/export/all?fmt=json|jsonl|csv|md|zip`.
  JSON keeps each page's text per source, CSV puts sources side by side (a column each), and ZIP bundles the JSON
  and CSV with a TXT and Markdown file per source. Like single-source export, it only reads cached results.
- Logos for LLM providers and OCR engines across the UI (bundled locally, no external requests).
- The upload file pickers now offer `.txt`, `.cbr` and `.pam` files, which the server already accepted.
- Fix: running Tesseract on two documents at once (e.g. two OCR jobs) failed with "Attempt to use Leptonica from
  2 threads at once"; Tesseract calls are now serialised across the whole process.
- LLM prompts and cache keys are unchanged, so existing results stay valid.

## 0.4.0 - 2026-11-27

- Several pages per LLM request: set **Pages per request** on the Overview page, `--pages-per-request N` on
  `pyocr extract`, or `pages_per_request` in the API's LLM body (1–20, default 1). Pages go out as labelled images
  in one call and the reply is split back into pages at `=== PAGE n ===` markers. A page the reply can't be matched
  to (marker missing or repeated, or cut off by the output limit) is re-sent on its own, with a warning. A batched
  run is its own source (`… · N pages/request`), so you can compare it with one page per request; existing
  single-page results and cache keys are unchanged. Tokens of a shared request are split evenly over its pages, and
  each page's record has a `batch` object with the request totals. New setting `LLM_BATCH_MAX_OUTPUT_TOKENS`
  (default 32000).
- Easier page selection on the Overview page. The page-range box checks what you type as you type ("9 of 14 pages",
  or what's wrong with the range) and stays in sync with a clickable page map (Shift-click for a range), with
  presets for all pages, the first 5, scans only, and pages with no text from the chosen engine yet. The map marks
  scans and pages that already have text. "Pages per request" has presets (1, 2, 3, 4, 5, 10), a box for any other
  value, and shows how many requests the job will make.
- **Adjust image** (page inspector, shortcut `e`): preprocess the page image before it is sent to an LLM, then
  check whether that helped. It has a strip of filters, each a thumbnail of the current page (Clean scan, High contrast,
  Black & white with Otsu thresholding, Fix faded, Denoise, Bring out detail, Invert), and fine-tuning for
  brightness, contrast, sharpness, denoise, threshold, resolution, rotation and crop. The viewer shows exactly the
  image the model receives, with its pixel and file size; *Hold to compare* shows the original.
  *A/B: original vs adjusted* transcribes both versions with the same model and diffs them, next to their token
  counts and timings. The settings are remembered per document and also apply to batch extraction from the Overview page.
- Preprocessing settings are part of the LLM cache key, so each setting is its own source to read, export and
  compare. Runs without preprocessing keep their existing keys, so cached results are unaffected.
- API: `preprocess` on `POST /api/documents/{id}/pages/{n}/llm` and in job bodies, `?preprocess=` on the page image
  endpoint, `GET /api/documents/{id}/pages/{n}/llm-image` (the exact image a model receives), `GET /api/preprocess`
  (validate / normalise a spec), and `preprocess_presets` in `/api/config`. CLI: `pyocr extract|compare --preprocess`.
- New dependency: Pillow (image processing).

## 0.3.0 - 2026-11-27

- Now on PyPI: `pip install pyocr-toolkit`. Releases are built, tested and published from GitHub Actions.

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

- Dark mode: a theme toggle in the top bar cycles system / light / dark. The choice is remembered, applied
  before the page paints (no flash), and "system" follows OS changes live. The whole UI was redesigned around one
  set of colour tokens with softer badges, ghost buttons and slimmer progress bars. The "Overview & extract" tab is
  now "Overview".
- Compare is now a diff studio: pick sources A and B (with swap), see CER, WER, pages under 2% and the worst page
  at a glance, click any bar of the colour-blind-safe per-page CER chart, and read that page's text in two
  aligned, scroll-synced columns. It opens on the worst page; the full table is still there.
- Library cards show native / OCR / LLM coverage bars and a status per document (all pages have text / needs
  text / scanned), with status filter chips and a total of pages still without text.
- Page inspector: new **Side by side** tab (key 5) showing the native, OCR and LLM text in synced columns.
- Project website on GitHub Pages, with animated tours of each view; the README shows the same tours.
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

- Developer task runner: `make` (or `python makefile.py`) lists grouped, rich-rendered commands for setup,
  linting/tests, running the app (incl. auto-reload `make dev`), building, Docker and housekeeping. `rich` is
  now a dev dependency.

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
