<div align="center">

<img src="https://raw.githubusercontent.com/utkarsh5026/pyocr/main/docs/logo.svg" width="72" height="72" alt="pyocr-toolkit logo">

# pyocr-toolkit

**See exactly what text you get out of a PDF or image, and compare how different extraction methods read it.**

[![CI](https://github.com/utkarsh5026/pyocr/actions/workflows/ci.yml/badge.svg)](https://github.com/utkarsh5026/pyocr/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776ab?logo=python&logoColor=white)
[![License](https://img.shields.io/badge/license-Apache%202.0-4f46e5)](https://github.com/utkarsh5026/pyocr/blob/main/LICENSE)
![Runs locally](https://img.shields.io/badge/runs-locally-15803d)

[**Website**](https://utkarsh5026.github.io/pyocr/) ·
[Install](#install) ·
[Tours](#tours) ·
[Providers](#configure-llm-providers) ·
[CLI](#command-line) ·
[How it works](#how-it-works)

<br>

<img src="https://raw.githubusercontent.com/utkarsh5026/pyocr/main/docs/tours/compare-light.webp" alt="Compare view, animated: Tesseract OCR scored against an LLM transcription of a scanned document, with the error rate per page and a word-level diff" width="100%">

<sub>▶ The <a href="https://utkarsh5026.github.io/pyocr/">website</a> opens with a 24-second video of what it does.</sub>

</div>

---

Many GenAI workflows start with "get the text out of this PDF". **pyocr-toolkit** is a local web app (plus CLI) for
checking that step. Upload a document and you get three versions of every page:

| | Source | What it is | Good at | Weak at |
|---|---|---|---|---|
| 📄 | **Native text** | Text already embedded in the file (PyMuPDF, or pypdf / pdfplumber / pdfminer.six / pypdfium2) | Born-digital PDFs. Exact and instant. | Scans have none. Broken font maps produce gibberish. |
| 🔍 | **OCR** | Tesseract, or any of 8 other OCR engines (below), on the rendered page image | Free/local or cheap cloud, gives word positions | Varies by engine: Tesseract struggles with tables, columns, poor scans, handwriting |
| ✨ | **LLM transcription** | A vision LLM told to copy the page *verbatim* | Hard pages, layout, context | Costs money. May quietly "fix" or skip text. |

Then you can:

- **Extract** page by page, a range like `1-5,9`, or the whole document, as background jobs
- **Compare** any two sources: character and word error rate (CER/WER), word-level diffs, a per-page chart
- **Search** across every document and every source at once
- **Export** clean text as TXT, Markdown, JSON or JSONL for your pipeline

**LLM providers:** Claude (Anthropic API), OpenAI, Azure OpenAI, Claude on Azure (Microsoft Foundry), Google
Gemini, and any OpenAI-compatible server (Ollama, vLLM, LM Studio, OpenRouter) for local or private models.

**OCR engines:** Tesseract (default), plus RapidOCR, EasyOCR, PaddleOCR and docTR running locally, and Google
Cloud Vision, Azure AI Document Intelligence, AWS Textract and Mistral OCR in the cloud. Each engine is its own
source, so you can score engines against each other or against an LLM.

**Inputs:** PDF, XPS, EPUB, CBZ, MOBI, FB2, SVG, PNG, JPEG, TIFF (multi-page), BMP, GIF, WebP and anything else
[PyMuPDF](https://pymupdf.readthedocs.io/) opens.

## Tours

Short recordings of the app on demo data (made by [`.claude/skills/demo/tour_shots.py`](https://github.com/utkarsh5026/pyocr/blob/main/.claude/skills/demo/tour_shots.py);
each is also in dark mode, as `docs/tours/*-dark.webp`).

<table>
  <tr>
    <td width="50%"><b>Page inspector</b>: a scanned page has no embedded text, so it's flagged <code>scan</code>; then its OCR
      and LLM text, all three side by side, and a diff.<br><br><img src="https://raw.githubusercontent.com/utkarsh5026/pyocr/main/docs/tours/inspector-light.webp" alt="Page inspector tour"></td>
    <td width="50%"><b>Compare</b>: Tesseract scored against an LLM across the document, a bar per page, and each page's
      diff.<br><br><img src="https://raw.githubusercontent.com/utkarsh5026/pyocr/main/docs/tours/compare-light.webp" alt="Compare tour"></td>
  </tr>
  <tr>
    <td><b>Search</b>: every document and every source at once; a hit opens on its page, highlighted on the image.<br><br><img src="https://raw.githubusercontent.com/utkarsh5026/pyocr/main/docs/tours/search-light.webp" alt="Search tour"></td>
    <td><b>Upload and extract</b>: drop a scan, run Tesseract on every page as a background job, read the result.<br><br><img src="https://raw.githubusercontent.com/utkarsh5026/pyocr/main/docs/tours/upload-light.webp" alt="Upload and extract tour"></td>
  </tr>
  <tr>
    <td colspan="2"><b>Library</b>: how much of each document has native, OCR and LLM text.<br><br><img src="https://raw.githubusercontent.com/utkarsh5026/pyocr/main/docs/tours/library-light.webp" alt="Library tour" width="50%"></td>
  </tr>
</table>

> [!NOTE]
> The LLM in these recordings is a local stand-in that answers with each sample page's true text (and "fixes" one
> misspelling, as real models do), so they can be remade without an API key. Native text and OCR are the real thing.

---

## Install

You need **Python 3.10+**. **Tesseract** is optional but recommended (it enables the OCR source).

```bash
# Tesseract (optional)
sudo apt-get install tesseract-ocr tesseract-ocr-eng   # Debian / Ubuntu
brew install tesseract                                  # macOS
choco install tesseract                                 # Windows (or the UB Mannheim installer)
```

**Option A: pipx** (recommended, installs it as an isolated command):

```bash
pipx install git+https://github.com/utkarsh5026/pyocr
```

**Option B: pip** in a virtual environment:

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install git+https://github.com/utkarsh5026/pyocr
```

> [!NOTE]
> The PyPI name is `pyocr-toolkit` (plain `pyocr` is a different, unrelated library). Once it's published to
> PyPI, `pipx install pyocr-toolkit` will also work.

**Option C: Docker** (Tesseract and several language packs included):

```bash
git clone https://github.com/utkarsh5026/pyocr && cd pyocr
cp .env.example .env            # add your keys
docker compose up -d            # → http://localhost:8000
```

## Run

```bash
pyocr init          # writes a .env template in the current folder: add the API keys you have
pyocr doctor        # checks Tesseract, providers and the data folder
pyocr serve --open  # starts the app on http://127.0.0.1:8000 and opens your browser
```

Everything you upload and extract is stored in `./data` (change it with `DATA_DIR`). The in-app **Help** page
(`/help`) covers every feature in detail.

## The web app

| Page | What you do there |
|---|---|
| **Home** `/` | Drop files to upload; see setup status and recent documents. |
| **Library** `/documents` | Document cards showing native / OCR / LLM coverage and how many pages still have no text. Filter by status, workspace or name; delete. |
| **Workspaces** `/workspaces` | Named groups of documents. Uploading several files or a `.zip` offers to create one, with suggested names (from the file names, or `adjective-scientist` like `festive-albattani`). Open one to rename it, add files or remove documents. |
| **Overview** `/documents/{id}` | Start background extraction (any OCR engine or LLM) for all pages or a range such as `1-5,9`, typed or picked on a clickable page map (Shift-click for a range; presets for scans only or pages without text yet); choose pages per LLM request; follow progress; see every *source* (engine + model + prompt) with token usage; export; page thumbnails. |
| **Page inspector** `/documents/{id}/pages/{n}` | Page image next to native, OCR and LLM text; run OCR or an LLM on just this page; per-page diff; a *Side by side* tab with all three texts in synced columns; search hits drawn on the image; *Adjust* to preprocess the image the LLM receives (filters, fine-tuning, crop/rotate) and A/B it against the original. Keyboard: `←/→`, `/`, `1-5`, `t`, `o`, `e`. |
| **Full text** `/documents/{id}/text` | Read the whole document from one source, optionally beside the page images; find, copy, download. |
| **Compare** `/documents/{id}/compare` | Score source B against source A on every page: document-level CER/WER, word errors split into misread / missed / added, how many numbers (amounts, dates, IDs) were read exactly, a clickable CER-per-page chart, and an aligned side-by-side diff of the selected page with a breakdown of its errors by kind (opens on the worst page). Works for OCR vs LLM, native vs LLM, or **model vs model**. |
| **Search** `/search` | Search all documents at once. |
| **Jobs** `/jobs` | All background extractions, with progress, errors and tokens. |
| **Providers** `/providers` | Which settings are set or missing, a form to set keys/endpoints in the browser, plus a **Test connection** button per provider. |
| **Models** `/models` | Search a catalog of vision models rated for transcription (excellent / good / fair / not suitable, with price and reason); list the models your key can use and pick one. |
| **Help** `/help` | The full guide: concepts, metrics, workflows, troubleshooting. |
| **API docs** `/docs` | Interactive OpenAPI docs for the JSON API. |

## Configure LLM providers

Paste keys on the **Providers** page (saved to `DATA_DIR/credentials.json`, owner-only, applied immediately and
taking precedence over the environment), or set them in `.env` (created by `pyocr init`) or as environment
variables and restart. Keys stay on the server and are never sent to the browser.

| Provider | Required | Optional |
|---|---|---|
| Claude (Anthropic API) | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` (default `claude-opus-5-5`), `ANTHROPIC_EFFORT` |
| OpenAI | `OPENAI_API_KEY` | `OPENAI_MODEL` (default `gpt-6.2-sol`), `OPENAI_BASE_URL` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` |
| Claude on Azure (Foundry) | `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_RESOURCE` | `ANTHROPIC_FOUNDRY_MODEL` |
| Google Gemini | `GEMINI_API_KEY` | `GEMINI_MODEL` (default `gemini-3.8-flash`) |
| Ollama / vLLM / LM Studio / OpenRouter | `OPENAI_COMPATIBLE_BASE_URL` | `OPENAI_COMPATIBLE_MODEL`, `OPENAI_COMPATIBLE_API_KEY` |

Any model or deployment name can be typed in the UI; the defaults are just starting points. The **Models** page
and the hint under each model box say whether a model suits transcription (custom names are marked as such). The model must accept
images. For a fully local setup, run Ollama with a vision model and set
`OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1`.

## Configure OCR engines

Tesseract is the default engine. Every other engine is optional, and the Providers page (and `pyocr engines`) shows
which are ready, with a **Test** button for each. Cloud keys and engine settings can be set there too, or as
environment variables. Local engines run on your machine; the first run downloads the
model weights. Cloud engines send the rendered page image to that service, and nowhere else.

| Engine | Kind | Install / required settings | Optional |
|---|---|---|---|
| Tesseract | local | system package (`apt install tesseract-ocr`, `brew install tesseract`) | `TESSERACT_LANGUAGE` |
| RapidOCR | local | `pip install "pyocr-toolkit[rapidocr]"` (ONNX Runtime, no PyTorch) | – |
| EasyOCR | local | `pip install "pyocr-toolkit[easyocr]"` (PyTorch) | `EASYOCR_LANGUAGES`, `OCR_GPU` |
| PaddleOCR | local | `pip install "pyocr-toolkit[paddleocr]"` (PaddlePaddle) | `PADDLEOCR_LANGUAGE` |
| docTR | local | `pip install "pyocr-toolkit[doctr]"` (PyTorch) | `OCR_GPU` |
| Google Cloud Vision | cloud | `GOOGLE_VISION_API_KEY` | `GOOGLE_VISION_LANGUAGE_HINTS` |
| Azure AI Document Intelligence | cloud | `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`, `AZURE_DOCUMENT_INTELLIGENCE_KEY` | – |
| AWS Textract | cloud | `pip install "pyocr-toolkit[textract]"`, `AWS_REGION` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (or `~/.aws`) |
| Mistral OCR | cloud | `MISTRAL_API_KEY` | `MISTRAL_OCR_MODEL` (default `mistral-ocr-latest`) |

Mistral OCR returns Markdown without word positions, so its search hits can't be drawn on the page image. In the
API, OCR sources are `ocr` (Tesseract) and `ocr:<engine>`, e.g. `ocr:easyocr`.

## PDF libraries

Native text comes from PyMuPDF, but Python has other PDF libraries, and they don't all read a text layer the same
way: reading order, spacing, ligatures and odd font encodings come out differently. If your pipeline uses one of
them, check *that* library on your PDFs. Each installed library is its own source, `native:<library>`:

| Library | Source | Notes |
|---|---|---|
| PyMuPDF | `native` | always installed; word boxes |
| pypdf | `native:pypdf` | pure Python; used by many LangChain / LlamaIndex loaders |
| pdfplumber | `native:pdfplumber` | built on pdfminer.six; word boxes |
| pdfminer.six | `native:pdfminer` | pure-Python layout analysis |
| pypdfium2 | `native:pypdfium2` | PDFium, the engine in Chrome |

Install them all with `pip install "pyocr-toolkit[pdf]"` (or just the ones you need). In the page inspector, the
**Native** tab has a library picker and a **Test all libraries** button that reads the page with each one and shows
whether it got text out, how much, and how fast. The Compare page and exports list every installed library, so you
can score `native:pypdf` against `native` across a whole document. `pyocr libraries` shows what's installed, and
`pyocr extract FILE --engine native:pdfplumber` extracts with one of them. They read PDF files only.

## Command line

```bash
pyocr extract report.pdf --engine llm -p anthropic --pages 1-5 --format jsonl -o report.jsonl
pyocr extract report.pdf --engine llm --pages-per-request 5  # 5 pages in each LLM request
pyocr extract scan.tiff --engine ocr --format md            # Tesseract, Markdown to stdout
pyocr extract scan.pdf --engine ocr:easyocr                  # any OCR engine (see `pyocr engines`)
pyocr extract born-digital.pdf --engine native              # embedded text only, no network
pyocr extract born-digital.pdf --engine native:pypdf        # the same text layer as pypdf reads it
pyocr compare scan.pdf -p openai -m gpt-6.2-sol                   # per-page OCR / native CER vs an LLM
pyocr extract scan.pdf -p anthropic --preprocess 'gray;auto;contrast=1.5'  # adjust the image first (or a preset: clean, bw, ...)
pyocr import a.pdf b.pdf scans.zip folder/ -w invoices      # add many files as one workspace (zips unpacked)
pyocr providers                                             # which providers are ready
pyocr engines                                               # which OCR engines are ready
python -m pyocr_toolkit --help                              # same CLI without the script on PATH
```

Documents processed on the command line also appear in the web app's library.

### Several pages per LLM request

If your pipeline sends a few pages in one LLM call, set **Pages per request** on the Overview page (or
`--pages-per-request N`, up to 20) to measure that setup. Each image goes out right after a label like `Page 5`, and the
model starts each page with `=== PAGE 5 ===`. The reply is split there, and each page is stored, scored and exported on
its own. A page whose marker is missing or repeated, or that the output limit cut off, is sent again on its own and
gets a warning, so text is never assigned to a page by guessing. The batch size is part of the prompt, so a batched
run is its own source: compare it with one page per request to see what batching costs in accuracy. Tokens of a
shared request are split evenly over its pages. Batching saves only the repeated system prompt (about 10% of input
tokens), so its main effect is fewer requests. `LLM_BATCH_MAX_OUTPUT_TOKENS` (default 32000) sets the output limit
for these requests.

### Batch uploads and workspaces

Upload several files, or a `.zip`, and they land together in a **workspace** whose name you can edit. pyocr suggests
names: the one the files share (`invoices-2024.zip` → `invoices-2024`) first, then short `adjective-scientist` ideas.
Zips are unpacked; files that can't be used (unsupported type, zip in a zip, encrypted, broken) are skipped and
reported with the reason. A single file still uploads straight to the library. Workspaces only reference documents
(`data/workspaces/<id>.json`), so removing a document from one, or deleting a workspace, keeps the document unless
you ask otherwise.

## How it works

1. **Native text.** A PDF page is a list of drawing instructions ("glyph 37 at (72, 700)"). Born-digital files map
   each glyph back to a character, so the text can be rebuilt exactly. A scan is only a picture, so it has no text.
2. **OCR.** The page is rendered to pixels (300 DPI) and an OCR engine (Tesseract by default) recognises characters
   from their shapes, reporting a box for each word (or line, split into words). That is why search can highlight OCR hits on the page.
3. **LLM.** The page image goes to a vision model with a strict "transcribe, don't correct" prompt
   ([`prompts.py`](https://github.com/utkarsh5026/pyocr/blob/main/src/pyocr_toolkit/llm/prompts.py)). It reads in context, so it is strong on hard pages, but it can
   "fix" typos, which the diff view exposes.
   Optionally the image is preprocessed first (grayscale, levels, contrast, denoise, black & white, crop, rotate;
   [`preprocess.py`](src/pyocr_toolkit/preprocess.py)). The settings are part of the cache key, so a run on the
   cleaned-up page is a separate source you can diff against the run on the original.
4. **Comparison.** *Edit distance* is the minimum number of character insertions, deletions and substitutions
   between two texts. **CER** = edits / reference characters; **WER** is the same count over words. For example,
   "the cat sat" → "the bat sat" has 1 edit, so CER = 1/11 = 9%. Whitespace is normalised first, because line
   breaks aren't reading errors. Disagreement between two engines is not proof of error: compare against a
   born-digital PDF's native text when you need ground truth.

The Help page explains each of these in more depth.

## Privacy and security

- Uploads, OCR output and LLM output are stored as plain files under `DATA_DIR`.
- A page image is sent to an LLM provider only when you start a transcription, and only to the provider you
  picked. Native text and Tesseract run entirely locally.

> [!WARNING]
> **There is no login.** `pyocr serve` listens on `127.0.0.1` by default. If you use `--host 0.0.0.0` or Docker,
> put it behind your own authentication.

## Development

```bash
git clone https://github.com/utkarsh5026/pyocr && cd pyocr
pip install -e ".[dev]"
pytest              # LLM providers are tested against mocked HTTP: no keys, no network
ruff check src tests
pyocr serve
```

Or use the task runner: `make` lists every developer command grouped by category (`make install`, `make check`,
`make dev` for auto-reload, `make docker-up`, ...). Without `make` (e.g. on Windows), run `python makefile.py <task>`.

See [CONTRIBUTING.md](https://github.com/utkarsh5026/pyocr/blob/main/CONTRIBUTING.md) for the code layout and how to add a provider.

The project website lives in [`docs/`](https://github.com/utkarsh5026/pyocr/tree/main/docs) (plain HTML and CSS, no build step) and is deployed to GitHub Pages by
[`.github/workflows/pages.yml`](https://github.com/utkarsh5026/pyocr/blob/main/.github/workflows/pages.yml) on every push to `main` that touches it. Its media are
made from code, on demo data that needs no API key:

```bash
python .claude/skills/demo/tour_shots.py        # docs/tours/*.webp: the app's tours, light and dark (needs Tesseract, Playwright)
cd hero-video && npm install && npm run render  # docs/media/hero-demo.mp4: the website's hero video (Remotion)
```

## License

Apache-2.0. See [LICENSE](https://github.com/utkarsh5026/pyocr/blob/main/LICENSE).
