# Changelog

## Unreleased

- Subtle UI motion: pages and card grids ease in, cards lift on hover, buttons respond to presses, tab panels
  and results fade in, and toasts fade out. While Tesseract or an LLM is working on a page, a scan line sweeps
  the page image, the button shows a spinner (with a flowing gradient for the model), the text box shimmers,
  and the finished text fades in. Running jobs get a pulsing badge and a moving progress bar. All of it turns
  off when the OS asks for reduced motion.

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
