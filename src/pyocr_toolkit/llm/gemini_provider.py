"""Google Gemini via the ``google-genai`` SDK."""

from __future__ import annotations

from google import genai
from google.genai import errors, types

from ..config import Settings
from . import catalog
from .base import Provider, ProviderError, TranscriptionRequest, TranscriptionResult


def _image_parts(req: TranscriptionRequest) -> list:
    """Image parts, each preceded by its label when the request holds several pages."""
    parts: list = []
    for page in req.page_images():
        if page.label:
            parts.append(page.label)
        parts.append(types.Part.from_bytes(data=page.image, mime_type=page.mime_type))
    return parts


class GeminiProvider(Provider):
    id = "gemini"
    label = "Google Gemini"
    brand = "gemini"
    docs_url = "https://aistudio.google.com/apikey"
    can_list_models = True
    env_vars = [
        ("GEMINI_API_KEY", True, "API key from Google AI Studio (GOOGLE_API_KEY also works)"),
        ("GEMINI_MODEL", False, "default model, e.g. gemini-3.8-flash"),
    ]
    suggested_models = catalog.recommended("gemini")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: genai.Client | None = None

    @property
    def api_key(self) -> str | None:
        return self.settings.gemini_api_key or self.settings.google_api_key

    @property
    def default_model(self) -> str | None:
        return self.settings.gemini_model

    def missing_settings(self) -> list[str]:
        return [] if self.api_key else ["GEMINI_API_KEY (or GOOGLE_API_KEY)"]

    def schema_support(self, model: str | None) -> tuple[str, str]:
        return "ok", f"{model or 'Gemini'} enforces this schema (response_json_schema)"

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def list_models(self) -> list[str] | None:
        try:
            pager = await self.client.aio.models.list(config={"page_size": 100})
            names = []
            async for m in pager:
                actions = m.supported_actions or []
                if actions and "generateContent" not in actions:
                    continue  # embeddings, TTS-only, ...
                names.append((m.name or "").removeprefix("models/"))
            return sorted(n for n in names if n)
        except (errors.ClientError, errors.ServerError) as exc:
            raise ProviderError(f"{self.label}: could not list models: {exc.code} {exc.message}") from exc

    async def _transcribe(self, req: TranscriptionRequest, model: str) -> TranscriptionResult:
        try:
            response = await self.client.aio.models.generate_content(
                model=model,
                contents=[*_image_parts(req), req.user_prompt],
                config=types.GenerateContentConfig(
                    system_instruction=req.system_prompt,
                    # Gemini 2.5 counts thinking tokens against this limit,
                    # so leave generous head-room.
                    max_output_tokens=max(req.max_output_tokens, 32768),
                    **({"response_mime_type": "application/json", "response_json_schema": req.output_schema}
                       if req.output_schema is not None else {}),
                ),
            )
        except errors.ClientError as exc:
            raise ProviderError(f"{self.label}: {exc.code} {exc.message}") from exc
        except errors.ServerError as exc:
            raise ProviderError(f"{self.label}: server error {exc.code} {exc.message}") from exc

        warnings: list[str] = []
        finish = None
        if response.candidates:
            finish = response.candidates[0].finish_reason
            finish = getattr(finish, "name", finish)
        if finish == "MAX_TOKENS":
            warnings.append("output hit the max token limit; transcription is truncated")
        elif finish in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise ProviderError(f"{self.label}: response blocked ({finish})")
        text = response.text or ""
        usage = response.usage_metadata
        return TranscriptionResult(
            text=text.strip(),
            provider=self.id,
            model=model,
            latency_ms=0,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
            stop_reason=str(finish) if finish else None,
            warnings=warnings,
        )
