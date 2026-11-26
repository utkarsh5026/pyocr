---
description: Run ruff and pytest, fix safe lint issues, and report results
allowed-tools: Bash(ruff:*), Bash(pytest:*), Bash(python -m pytest:*), Bash(which:*), Read, Edit
---

Run the project's quality gate and get it green.

1. `ruff check src tests`. If it fails, run `ruff check --fix src tests` for auto-fixable issues, then fix the rest
   by hand. Don't add `# noqa` or change `[tool.ruff]` config to silence real problems.
2. `pytest -q $ARGUMENTS` (arguments, if given, narrow the run, e.g. `tests/test_core.py` or `-k compare`).
3. If tests fail, read the failing test and the code under test, find the root cause, and fix the code, not the
   test, unless the test is clearly wrong. Re-run until green.
4. Check `which tesseract`. If it's missing, the OCR tests were skipped: say so explicitly.

Report: lint status, pass/fail/skip counts, and what you changed (file + one line each).
