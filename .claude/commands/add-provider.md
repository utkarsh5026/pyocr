---
description: Add a new LLM provider end to end
argument-hint: <provider name, e.g. "Mistral" or "AWS Bedrock Claude">
---

Add an LLM provider for: **$ARGUMENTS**

Read first: `src/pyocr_toolkit/llm/base.py`, an existing provider closest to the new one
(`openai_provider.py` for OpenAI-style APIs, `anthropic_provider.py` for Claude-style, `gemini_provider.py`
otherwise), `src/pyocr_toolkit/config.py`, `src/pyocr_toolkit/llm/__init__.py`, and `tests/test_providers.py`.

If the provider speaks the OpenAI chat-completions protocol, ask whether the existing OpenAI-compatible provider
already covers it before writing a new class.

Steps:
1. Look up the provider's current API docs (auth, image input format, max tokens, stop reasons, truncation
   signal). Don't guess model ids or request shapes.
2. Create the provider class (in an existing module if it's the same SDK family, otherwise a new
   `llm/<name>_provider.py`). Set `id` (stable, lowercase: it becomes part of cache keys), `label`,
   `env_vars`, `suggested_models`, `model_hint`, `docs_url`.
3. Implement `default_model`, `missing_settings()`, and `async _transcribe(req, model)`:
   - send `req.system_prompt`, `req.user_prompt` and the image (`req.image`, `req.mime_type`)
   - respect `req.max_output_tokens` and `settings.llm_timeout_s`
   - return `TranscriptionResult` with token counts and `stop_reason`
   - append a warning when output was truncated or refused
   - wrap SDK/HTTP errors in `ProviderError` with a message that says what to fix
4. Add settings to `config.py`, then the same lines to **both** `.env.example` and
   `src/pyocr_toolkit/env.example` (they must be byte-identical).
5. Register the class in `PROVIDER_CLASSES` in `llm/__init__.py`. If it needs a new SDK, add it to
   `pyproject.toml` dependencies.
6. Add mocked-HTTP tests in `tests/test_providers.py` following the existing pattern: request shape, response
   parsing, truncation warning, auth error. No real network.
7. Update the README provider table and the Help page if it lists providers. Add a CHANGELOG line.
8. Run `ruff check src tests && pytest -q`.

Finish with a short summary: files changed, env vars added, default model, and anything you couldn't verify.
