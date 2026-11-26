---
description: Add a new server-rendered page to the web app
argument-hint: <page name and what it does>
---

Add a web page: **$ARGUMENTS**

Read first: `src/pyocr_toolkit/web.py`, `templates/base.html`, one similar existing template + its
`static/<page>.js`, `static/common.js`, and `test_html_pages_render` in `tests/test_web.py`.

Steps:
1. Route in `build_web()` in `web.py`, rendered with the local `page(request, template, nav, **ctx)` helper.
   Data the page fetches after load should come from an `/api` endpoint in `api.py`; put shared logic in
   `service.py`.
2. Template `templates/<name>.html` extending `base.html`. Reuse partials (`_icons.html`, `_doctabs.html` for
   per-document pages, `_llm_picker.html` for provider/model selection).
3. If it's a top-level page, add a link in the `mainnav` of `base.html` with its `nav` id.
4. Script `static/<name>.js`, loaded via `{% block scripts %}<script src="/static/<name>.js?v={{ version }}"></script>`.
   Vanilla JS only, no libraries, no build step. Use `common.js` helpers and `esc()` all user/document text before
   inserting into HTML. Wrap async handlers in `guard()`.
5. Styles go in `static/styles.css` using the existing CSS variables, so light and dark themes both work.
6. Remember page numbers are 0-based in the API and 1-based in URLs/UI.
7. Add the URL and a distinctive needle string to `test_html_pages_render`, and the JS file to its static asset
   list.
8. Document the page in `templates/help.html` and the README "The web app" table.
9. Run `ruff check src tests && pytest -q`. Then start the app (`/smoke` or `pyocr serve`) and load the page
   to confirm it renders without console errors.
