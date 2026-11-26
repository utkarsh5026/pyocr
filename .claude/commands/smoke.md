---
description: Start the app against a throwaway data dir and hit every page and key API route
allowed-tools: Bash
---

Smoke-test the running app.

1. Pick a free port (try 8765, then 8766...). Make a temp data dir (use the session scratchpad if one exists,
   otherwise `mktemp -d`). Never use the repo's `./data`.
2. Start in the background: `DATA_DIR=<tmp> pyocr serve --port <port>` (or
   `python -m pyocr_toolkit serve --port <port>`). Wait until `curl -sf http://127.0.0.1:<port>/api/config`
   succeeds (up to ~30 s).
3. Generate a small 2-page PDF with PyMuPDF (one page with real text, one page that is only an image of text)
   and upload it: `curl -sf -F file=@sample.pdf http://127.0.0.1:<port>/api/documents`. Note the returned `id`.
4. Check each returns 200: `/`, `/documents`, `/documents/<id>`, `/documents/<id>/pages/1`,
   `/documents/<id>/text`, `/documents/<id>/compare`, `/search?q=fox`, `/jobs`, `/providers`, `/help`, `/docs`,
   and every file under `src/pyocr_toolkit/static/`.
5. If `tesseract` is installed, run OCR on page 2 through the API and confirm text comes back.
6. Stop the server and delete the temp dir.

$ARGUMENTS

Report a table of route → status, plus any server tracebacks from the log.
