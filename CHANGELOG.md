# Changelog

## Unreleased

- **More OCR engines.** Besides Tesseract: RapidOCR, EasyOCR, PaddleOCR and docTR (local, optional extras such as
  `pip install "pyocr-toolkit[easyocr]"`), and Google Cloud Vision, Azure AI Document Intelligence, AWS Textract
  and Mistral OCR (cloud, configured with environment variables). Each engine is a separate source
  (`ocr:<engine>`; `ocr` stays Tesseract) that can be searched, compared and exported.
- Page inspector, Overview extract form, background jobs and the CLI (`--engine ocr:<engine>`, `--ocr-engine`,
  `pyocr engines`) can pick the OCR engine. The Providers page lists every engine with its status and a Test button.
- Logos for LLM providers and OCR engines across the UI (bundled locally, no external requests).
- Existing Tesseract results (`ocr/<page>.json`) are read as before; other engines cache under `ocr/<engine>/`.

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
