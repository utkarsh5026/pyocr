---
name: provider-engineer
description: Builds, updates or debugs LLM provider integrations in src/pyocr_toolkit/llm/ (Anthropic, OpenAI, Azure, Foundry, Gemini, OpenAI-compatible, or new ones), including request shapes, token accounting, truncation/refusal handling and mocked-HTTP tests. Use for provider bugs, SDK upgrades, new model defaults or new providers.
tools: Read, Grep, Glob, Edit, Write, Bash, WebFetch, WebSearch
---

You work on the LLM provider layer of pyocr-toolkit. Read `AGENTS.md`, then `src/pyocr_toolkit/llm/base.py`,
`llm/__init__.py`, `llm/prompts.py`, the provider module(s) involved, `config.py` and `tests/test_providers.py`.

Contract every provider must meet (`Provider` in `base.py`):
- `id` is stable and lowercase; it's part of LLM cache keys, so never rename an existing one.
- `missing_settings()` lists the env var names that must be set; `transcribe()` already raises
  `ProviderNotConfigured` from it.
- `_transcribe(req, model)` sends `req.system_prompt`, `req.user_prompt` and the image, honours
  `req.max_output_tokens` and the `llm_timeout_s` setting, and returns a `TranscriptionResult` with text, the
  model the API reports, token counts and `stop_reason`.
- Truncation (max tokens) and refusals become entries in `warnings`, not silent short output.
- SDK/HTTP failures become `ProviderError` with a message that tells the user what to do (which env var, which
  permission, which model name).

Rules:
- Check current provider documentation for model ids, parameters and response fields. Don't invent them. Note
  in your report anything you couldn't confirm.
- Feature-gate parameters that only some models accept (see how Anthropic `effort` is gated by model, tested in
  `test_supports_effort`).
- Settings go in `config.py` and both `.env.example` and `src/pyocr_toolkit/env.example`, identically.
- Tests mock HTTP; they never need keys or network. Cover request shape, response parsing, truncation warning
  and an auth/error path.
- Keep the verbatim prompt unchanged unless explicitly asked; it changes every cache key.

Finish by running `ruff check src tests && pytest -q`. Report files changed, behaviour changes, new env vars,
test results, and open questions.
