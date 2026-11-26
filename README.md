# pyocr-toolkit

**See exactly what text you get out of a PDF or image, and compare how different extraction methods read it.**

Many GenAI workflows start with "get the text out of this PDF". pyocr-toolkit is a local web app (plus CLI)
for checking that step. Upload a document and you get three versions of every page:

| Source | What it is | Good at | Weak at |
|---|---|---|---|
| **Native text** | Text already embedded in the file (PyMuPDF) | Born-digital PDFs. Exact and instant. | Scans have none. Broken font maps produce gibberish. |
| **Tesseract OCR** | Classic OCR on the rendered page image | Free, local, fast, gives word positions | Tables, columns, poor scans, handwriting |
| **LLM transcription** | A vision LLM told to copy the page *verbatim* | Hard pages, layout, context | Costs money. May quietly "fix" or skip text. |

You can then extract **page by page or the whole document**, **compare** any two sources (character and word error
rate, word-level diffs, a per-page chart), **search** across everything, and **export** clean text (TXT, Markdown,
JSON, JSONL) for your pipeline.

**LLM providers:** Claude (Anthropic API), OpenAI, Azure OpenAI, Claude on Azure (Microsoft Foundry), Google
Gemini, and any OpenAI-compatible server (Ollama, vLLM, LM Studio, OpenRouter) for local or private models.

**Inputs:** PDF, XPS, EPUB, CBZ, MOBI, FB2, SVG, PNG, JPEG, TIFF (multi-page), BMP, GIF, WebP and anything else
[PyMuPDF](https://pymupdf.readthedocs.io/) opens.

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
| **Library** `/documents` | All documents, with how much has been OCR'd / transcribed. Filter and delete. |
| **Overview** `/documents/{id}` | Start background extraction (Tesseract or any LLM) for all pages or a range such as `1-5,9`; follow progress; see every *source* (engine + model + prompt) with token usage; export; page thumbnails. |
| **Page inspector** `/documents/{id}/pages/{n}` | Page image next to native, OCR and LLM text; run OCR or an LLM on just this page; per-page diff; search hits drawn on the image. Keyboard: `←/→`, `/`, `1-4`, `t`, `o`. |
| **Full text** `/documents/{id}/text` | Read the whole document from one source, optionally beside the page images; find, copy, download. |
| **Compare** `/documents/{id}/compare` | Score source B against source A on every page: document-level CER/WER, CER per page chart, worst pages, word diff for any page. Works for OCR vs LLM, native vs LLM, or **model vs model**. |
| **Search** `/search` | Search all documents at once. |
| **Jobs** `/jobs` | All background extractions, with progress, errors and tokens. |
| **Providers** `/providers` | Which settings are set or missing, plus a **Test connection** button per provider. |
| **Help** `/help` | The full guide: concepts, metrics, workflows, troubleshooting. |
| **API docs** `/docs` | Interactive OpenAPI docs for the JSON API. |

## Configure LLM providers

Set these in `.env` (created by `pyocr init`) or as environment variables, then restart. Keys stay on the server and
are never sent to the browser.

| Provider | Required | Optional |
|---|---|---|
| Claude (Anthropic API) | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` (default `claude-opus-5-5`), `ANTHROPIC_EFFORT` |
| OpenAI | `OPENAI_API_KEY` | `OPENAI_MODEL` (default `gpt-5`), `OPENAI_BASE_URL` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` |
| Claude on Azure (Foundry) | `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_RESOURCE` | `ANTHROPIC_FOUNDRY_MODEL` |
| Google Gemini | `GEMINI_API_KEY` | `GEMINI_MODEL` (default `gemini-2.5-flash`) |
| Ollama / vLLM / LM Studio / OpenRouter | `OPENAI_COMPATIBLE_BASE_URL` | `OPENAI_COMPATIBLE_MODEL`, `OPENAI_COMPATIBLE_API_KEY` |

Any model or deployment name can be typed in the UI; the defaults are just starting points. The model must accept
images. For a fully local setup, run Ollama with a vision model and set
`OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1`.

## Command line

```bash
pyocr extract report.pdf --engine llm -p anthropic --pages 1-5 --format jsonl -o report.jsonl
pyocr extract scan.tiff --engine ocr --format md            # Tesseract, Markdown to stdout
pyocr extract born-digital.pdf --engine native              # embedded text only, no network
pyocr compare scan.pdf -p openai -m gpt-5                   # per-page OCR / native CER vs an LLM
pyocr providers                                             # which providers are ready
python -m pyocr_toolkit --help                              # same CLI without the script on PATH
```

Documents processed on the command line also appear in the web app's library.

## How it works, briefly

1. **Native text.** A PDF page is a list of drawing instructions ("glyph 37 at (72, 700)"). Born-digital files map
   each glyph back to a character, so the text can be rebuilt exactly. A scan is only a picture, so it has no text.
2. **OCR.** The page is rendered to pixels (300 DPI) and Tesseract recognises characters from their shapes,
   reporting a box for each word. That is why search can highlight OCR hits on the page.
3. **LLM.** The page image goes to a vision model with a strict "transcribe, don't correct" prompt
   ([`prompts.py`](src/pyocr_toolkit/llm/prompts.py)). It reads in context, so it is strong on hard pages, but it can
   "fix" typos, which the diff view exposes.
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
- **There is no login.** `pyocr serve` listens on `127.0.0.1` by default. If you use `--host 0.0.0.0` or Docker,
  put it behind your own authentication.

## Development

```bash
git clone https://github.com/utkarsh5026/pyocr && cd pyocr
pip install -e ".[dev]"
pytest              # LLM providers are tested against mocked HTTP: no keys, no network
ruff check src tests
pyocr serve
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the code layout and how to add a provider.

## License

Apache-2.0. See [LICENSE](LICENSE).
