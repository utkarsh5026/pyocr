---
description: Release a version end to end - bump, changelog, checks, commit, tag, GitHub Release (which publishes to PyPI)
argument-hint: <version, e.g. 0.3.0> [--dry-run] [--testpypi]
---

Release **$ARGUMENTS** of `pyocr-toolkit`.

The GitHub Release is what publishes: `.github/workflows/release.yml` runs on `release: published`, re-runs lint,
tests and the wheel smoke test, then uploads to PyPI with Trusted Publishing. So this command ends by creating the
release and watching that workflow, never by running `twine` itself.

Flags:

- `--dry-run`: do steps 1–4 only (local changes and checks), then stop. Nothing is committed or pushed.
- `--testpypi`: after pushing the release commit, upload to TestPyPI first (step 7) and stop if that fails.

## 1. Preflight (stop and report if any check fails)

Run these and stop on the first failure, saying what to do about it:

- `git rev-parse --abbrev-ref HEAD` is `main`, and `git status --porcelain` is empty.
- `git fetch origin --tags` then `git status -sb`: `main` isn't behind `origin/main`. If it is, ask before pulling.
- `gh auth status` succeeds.
- The version is `X.Y.Z` (PEP 440 pre-releases like `0.3.0rc1` are fine) and is either:
  - greater than `__version__` in `src/pyocr_toolkit/__init__.py`, or
  - equal to it, as long as tag `v<version>` doesn't exist yet. This is a version that was prepared but never
    released, so skip the bump in step 2.
- Tag `v<version>` doesn't exist locally (`git tag -l`) or on origin (`git ls-remote --tags origin`).
- PyPI doesn't have it: `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/pyocr-toolkit/<version>/json`
  must be `404`. PyPI never accepts a version twice, even a deleted one.
- `git diff --quiet origin/main -- .github/workflows/release.yml` and the file exists on `origin/main`. The workflow
  runs from the tagged commit, so it has to be pushed before the release.

## 2. Bump the version

Set the same version in `src/pyocr_toolkit/__init__.py` (`__version__`) and `pyproject.toml` (`version`).
Then run `python makefile.py version` to confirm they agree.

## 3. Changelog

Read `CHANGELOG.md` and `git log --oneline v<previous>..HEAD` (if there's no previous tag, use the log since the
commit that set the previous version).

- Rename `## Unreleased` to `## <version> - <YYYY-MM-DD>` (today's date) and add a fresh empty `## Unreleased`
  above it. If `## <version>` already exists (a prepared release), update that section instead.
- Edit the bullets so they're user-facing and grouped by feature, not a commit list. Fill gaps from the git log:
  new env vars, changed defaults, new pages or API routes, and anything that changes cached results (prompt text or
  `llm_cache_key` changes). Keep the existing style.
- If there's nothing user-visible since the last release, stop and ask whether to release anyway.

Write the section body (without the heading) to `<scratchpad>/release-notes-<version>.md`. It becomes the GitHub
Release notes, followed by an install line: `pip install pyocr-toolkit==<version>`.

## 4. Checks and build

- `ruff check src tests && pytest -q`. Say if the OCR tests were skipped (no `tesseract` on PATH).
- `python makefile.py clean`, then `python -m build`, then `python -m twine check --strict dist/*`.
- Install `dist/*.whl` into a fresh venv in the scratchpad, then run `pyocr --version` from outside the repo.
  It must print the new version.
- If the README uses relative `src="docs/..."` or `](docs/...)` paths, warn me. They break on PyPI.

If `--dry-run` was given, stop here. Show the diff, the release notes and the commit message, and say nothing was
committed.

## 5. Confirm (the only stop before anything leaves this machine)

Show:

- `git diff --stat` and the changelog section.
- The commit message `Release v<version>`, with the attribution trailer.
- What happens next: push to `origin/main`, tag `v<version>`, create the GitHub Release, PyPI upload.

Ask me to confirm, and go on only if I say yes. A yes covers steps 6–9.

## 6. Commit and push

```bash
git add src/pyocr_toolkit/__init__.py pyproject.toml CHANGELOG.md
git commit -m "Release v<version>" -m "<attribution trailer>"
git push origin main
```

`main` blocks force pushes and deletion, not direct pushes. If the push is rejected anyway (new rules, or `main`
moved), don't force it. Instead:

1. Push a `release/v<version>` branch.
2. Open a PR with `gh pr create` and the release notes as its body.
3. Stop and tell me to merge it, then rerun `/release <version>`. Preflight will see the merged version and skip
   the bump.

## 7. TestPyPI (only with `--testpypi`)

```bash
gh workflow run release.yml --ref main
gh run list --workflow release.yml --event workflow_dispatch --limit 1 --json databaseId   # wait a few seconds first
gh run watch <id> --exit-status
```

When it passes, install the package in a fresh scratchpad venv:
`pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple pyocr-toolkit==<version>`.
Check `pyocr --version`. If the run fails, stop and report the failing step from `gh run view <id> --log-failed`.

## 8. Tag and create the GitHub Release

```bash
git tag -a v<version> -m "v<version>"
git push origin v<version>
gh release create v<version> --verify-tag --title "v<version>" \
  --notes-file <scratchpad>/release-notes-<version>.md dist/*
```

Add `--prerelease` when the version has `a`, `b` or `rc` in it. The `dist/*` files are attached to the release for
people who want the wheel without PyPI; PyPI gets its own clean build from the workflow.

## 9. Watch the publish and verify

- Find the run: `gh run list --workflow release.yml --event release --limit 1 --json databaseId,headBranch`.
  Watch it with `gh run watch <id> --exit-status`.
- When it passes, poll `https://pypi.org/pypi/pyocr-toolkit/<version>/json` (it can take a minute) until it returns
  200. Then install `pyocr-toolkit==<version>` from PyPI into a fresh scratchpad venv and run `pyocr --version`.
- If it fails, show the failing step from `gh run view <id> --log-failed` and diagnose it:
  - **`invalid-publisher` / OIDC error**: the trusted publisher isn't set up. Tell me to add it on pypi.org: owner
    `utkarsh5026`, repo `pyocr`, workflow `release.yml`, environment `pypi`. After that, `gh run rerun <id> --failed`
    finishes the release. No new tag is needed.
  - **Lint/test/build failure**: nothing reached PyPI. Fix it in a follow-up commit and ask me whether to move the
    tag or release the next patch version. Never delete or move a published tag, or delete a release, without asking.
  - **Version already exists on PyPI**: bump to the next patch version. PyPI versions can't be reused.

## Report

The version, the commit SHA, the release URL (`gh release view v<version> --json url`), the PyPI URL
`https://pypi.org/project/pyocr-toolkit/<version>/`, and whether the fresh install worked. Also say whether OCR tests
were skipped locally, and whether the README still installs from `git+https://...` instead of `pip install
pyocr-toolkit`.
