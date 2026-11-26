---
description: Prepare a release - bump version, write the changelog, run checks
argument-hint: <new version, e.g. 0.3.0>
---

Prepare release **$ARGUMENTS**. Don't tag, push or publish unless I ask.

1. Confirm the version is a valid bump over the current one in `src/pyocr_toolkit/__init__.py`.
2. Set it in both `src/pyocr_toolkit/__init__.py` (`__version__`) and `pyproject.toml` (`version`).
3. Read `git log` since the last version bump and the existing `CHANGELOG.md`. Add a `## <version>` section at
   the top in the same style: user-facing bullets grouped by feature, not a commit list. Mention new env vars,
   changed defaults, and anything that affects cached results (prompt or cache-key changes).
4. Run `ruff check src tests && pytest -q`.
5. Build and check the wheel installs: `python -m build` and install `dist/*.whl` into a fresh venv, then run
   `pyocr --version`.
6. Show me the diff and the proposed commit message.
