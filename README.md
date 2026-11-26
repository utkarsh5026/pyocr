# pyocr: OCR inspector

Upload a PDF or an image and check how well its text gets extracted. For each page you can see three
versions of the text next to the page image:

| Source | What it is | When it is right |
|---|---|---|
| **Native text** | The text the PDF already contains (PyMuPDF `page.get_text()`) | Born-digital PDFs (exported from Word, LaTeX, ...). Scanned pages have none. |
| **Tesseract OCR** | Tesseract run on the rendered page image (`page.get_textpage_ocr`) | The classic OCR baseline. Weak on low-quality scans, tables and handwriting. |
| **LLM transcription** | A vision LLM (Claude, GPT, Gemini, ...) asked to copy the page **verbatim** | Usually the most accurate reading of the page, but costs money and can occasionally "fix" text it shouldn't. |

You can then **compare** any two of them (character and word error rate plus a word-level diff), **search**
the whole document (matches are highlighted on the page image), and run OCR or an LLM over **every page** to
get a per-page report showing which pages the OCR struggles on.

Supported inputs are anything PyMuPDF opens: PDF, XPS, EPUB, CBZ, MOBI, FB2, SVG, and raster images (PNG, JPEG,
TIFF including multi-page, BMP, GIF, WebP, ...).

Supported LLM providers:

| Provider id | Service | Settings |
|---|---|---|
| `anthropic` | Claude via the Anthropic API | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-opus-5-5`) |
| `openai` | OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-5`) |
| `azure_openai` | Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` |
| `azure_claude` | Claude on Microsoft Foundry (Azure) | `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_RESOURCE` |
| `gemini` | Google Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL` |
| `openai_compatible` | Ollama, vLLM, LM Studio, OpenRouter, ... | `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_MODEL` |

The model can also be changed per request in the UI. Whatever you type there is sent as-is; for Azure, it is
the deployment name.

## Quick start

```bash
# 1. system dependency for the OCR tab (optional; the rest works without it)
sudo apt-get install tesseract-ocr tesseract-ocr-eng     # macOS: brew install tesseract

# 2. install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 3. configure the providers you have keys for
cp .env.example .env    # then edit .env

# 4. run
pyocr serve             # http://127.0.0.1:8000  (use --host 0.0.0.0 to share on your network)
```

Command-line use without the UI:

```bash
pyocr providers                                    # which providers are configured
pyocr transcribe scan.pdf -p anthropic --pages 1-3 # print LLM transcriptions
pyocr compare scan.pdf -p openai -m gpt-5          # per-page OCR/native error rate vs the LLM
```

## Using the UI

1. **Upload**: use the button, or drag a file onto the window. Earlier uploads appear in the drop-down.
2. **Page list** (left): thumbnails with badges. `scan` means the page has no real text layer, so a plain
   text extractor would get nothing from it. `OCR` and `LLM` show what has already been run.
3. **Tabs** (right):
   * *Native text*: what a non-OCR pipeline sees.
   * *Tesseract OCR*: click **Run OCR**. Results are cached on disk.
   * *LLM*: choose provider, model and prompt in the top bar, then click **Transcribe page**. Results are
     cached per (provider, model, prompt), so reopening a page costs nothing. Use **re-run** to call the
     model again.
   * *Compare*: pick a reference and a candidate. The usual setup is LLM as the reference and OCR as the
     candidate.
4. **Search** (`/` focuses it): searches native text, cached OCR and stored LLM transcriptions. Native and OCR
   hits are drawn on the page (yellow = native, blue = OCR). LLM hits show as snippets, because an LLM returns
   plain text with no coordinates.
5. **OCR all pages / LLM all pages**: batch runs with a progress bar. **Report** shows a per-page table of
   error rates and lets you export each source as `.txt` or `.json`.

Keyboard: `←`/`→` (or `j`/`k`) change page, `/` searches.

## How it works

### 1. Where text in a PDF comes from

A PDF page does not store "text" the way a `.txt` file does. It stores **drawing instructions**: "use font F
at size 12, place glyph #37 at (72, 700), then glyph #81 ...". When a PDF is exported from Word, each glyph is
also mapped back to a Unicode character, so `page.get_text()` can rebuild words and lines from glyph positions.
That is the **native text layer**. It is exact, because nothing is guessed, but only if:

* the page was born digital. A scanner only produces a *picture* of the page, so there are no glyphs.
* the font's glyph-to-Unicode map is correct. Some PDFs map glyphs to garbage, which is why native text can be
  wrong even when it looks fine on screen.

The `looks_scanned` badge marks pages that have no text layer, or almost none, but do contain images.

### 2. OCR: turning pixels back into characters

For a scan, the only thing we have is pixels. PyMuPDF renders the page to a bitmap at `OCR_DPI` (300 by
default; small text needs enough pixels per character) and gives it to **Tesseract**. Tesseract finds lines,
segments them into characters, and classifies each one with an LSTM network. It returns words with bounding
boxes, which is how search can highlight OCR hits on the page. It is fast and free, but it reads shape by
shape and has little sense of layout or meaning. Tables, stamps, skewed or low-contrast scans and handwriting
are where it fails.

### 3. LLM transcription

A vision LLM gets the same kind of page image (rendered at `LLM_DPI`, and downscaled so the longest edge is
at most `LLM_MAX_EDGE_PX`, because the providers downscale large images themselves and a bigger upload just
costs time). It also gets a system prompt that tells it to act as a transcription engine rather than an
assistant. See [`src/pyocr/llm/prompts.py`](src/pyocr/llm/prompts.py):

* **verbatim**: plain text, line by line, no spelling fixes, tables as `cell | cell`. Use this mode to judge
  OCR, because both outputs then have the same shape.
* **markdown**: keeps structure (headings, GFM tables). Use it to see what an LLM pipeline downstream would
  receive.
* **custom**: your own prompt, for example "extract only the line items".

Because the model reads whole words and sentences in context, it is usually much more accurate than Tesseract
on hard pages. It can also make errors OCR never makes: silently "correcting" a typo, normalising a date,
skipping a repeated header. Use the diff view to spot these.

Claude-specific details (in [`anthropic_provider.py`](src/pyocr/llm/anthropic_provider.py)): requests are
streamed so long pages don't hit HTTP timeouts. `ANTHROPIC_EFFORT` sets how much the model reasons before
answering. For recent first-party models the request enables the **server-side refusal fallback**: if a
safety classifier declines a page, the API re-runs it on a fallback model instead of returning nothing. The
UI shows a warning when that happens.

### 4. Measuring agreement: CER and WER

To check whether OCR read a page correctly, compare its output with a trusted transcription. The standard
measure is **edit distance** (Levenshtein): the minimum number of single-character insertions, deletions
and substitutions that turn one string into the other.

```
reference : "the cat sat"
candidate : "the bat sat"      -> 1 substitution
CER = edits / reference length = 1 / 11 = 9.1 %
WER = word edits / reference words = 1 / 3 = 33 %
```

* **CER** (character error rate) is what OCR quality is usually quoted in. Below about 2 % is very good, and
  above about 10 % will hurt downstream extraction (green/amber/red in the UI).
* **WER** is harsher: one wrong letter makes the whole word wrong. It is closer to what matters for search and
  for exact values like invoice numbers.

Whitespace is collapsed before scoring, because OCR engines and LLMs break lines differently and that is not a
reading error. Ligatures (`ﬁ` → `fi`) are normalised and end-of-line hyphenation is joined. Case and
punctuation can be ignored with the checkboxes. The diff is computed on words, aligned with the same edit
distance, and shown as missing (red), extra (green) or replaced words.

Note that "LLM vs OCR" measures **disagreement**, not ground truth. If you have a born-digital PDF, its native
text is real ground truth: compare OCR and the LLM against it to see how each engine performs on your kind of
documents.

## Storage and privacy

Everything is stored under `DATA_DIR` (default `./data`, which git ignores):

```
data/documents/<id>/source.pdf        the original upload
data/documents/<id>/ocr/<page>.json   Tesseract output + word boxes
data/documents/<id>/llm/<page>/*.json LLM transcriptions (text, model, tokens, latency)
```

Page images are sent **only** to the LLM provider you choose, and only when you click transcribe. API keys are
read from the server environment and never sent to the browser. There is no authentication, so run it locally
or behind your own auth proxy.

## HTTP API

The UI is a thin client over a JSON API (interactive docs at `/docs`):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/documents` | upload (multipart `file`) |
| `GET` | `/api/documents/{id}/pages` | per-page info (size, text chars, scanned?, cached results) |
| `GET` | `/api/documents/{id}/pages/{n}/image?dpi=` | page PNG |
| `GET` | `/api/documents/{id}/pages/{n}/text/native` | text layer + word boxes |
| `GET` | `/api/documents/{id}/pages/{n}/text/ocr?force=` | Tesseract text + word boxes (cached) |
| `POST` | `/api/documents/{id}/pages/{n}/llm` | `{provider, model, prompt_mode, custom_prompt, force}` |
| `POST` | `/api/documents/{id}/pages/{n}/compare` | `{reference:{source,key}, hypothesis:{...}}` → metrics + diff |
| `GET` | `/api/documents/{id}/search?q=&sources=native,ocr,llm` | matches with boxes and snippets |
| `GET` | `/api/documents/{id}/report` | per-page error rates |
| `GET` | `/api/documents/{id}/export?source=llm&fmt=txt` | whole-document text |

Page numbers are 0-based in the API.

## Development

```bash
pip install -e ".[dev]"
pytest            # provider tests use mocked HTTP, so no keys or network are needed
```

To add another provider, subclass `pyocr.llm.base.Provider`, implement `default_model`,
`missing_settings()` and `_transcribe()`, then register it in `pyocr/llm/__init__.py`.
