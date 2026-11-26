"""OpenAI, Azure OpenAI and any OpenAI-compatible server (Ollama, vLLM, LM Studio, OpenRouter...)."""

from __future__ import annotations

import base64

import openai

from ..config import Settings
from . import catalog
from .base import Provider, ProviderError, TranscriptionRequest, TranscriptionResult


class _OpenAIChatBase(Provider):
    #: OpenAI's newer models want ``max_completion_tokens``; many
    #: OpenAI-compatible servers only understand the older ``max_tokens``.
    token_param = "max_completion_tokens"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    def _make_client(self):
        raise NotImplementedError

    @property
    def client(self):
        if self._client is None:
            self._client = self._make_client()
        return self._client

    async def list_models(self) -> list[str] | None:
        try:
            return sorted([m.id async for m in self.client.models.list()])
        except openai.APIError as exc:
            raise ProviderError(f"{self.label}: could not list models: {exc}") from exc

    async def _transcribe(self, req: TranscriptionRequest, model: str) -> TranscriptionResult:
        parts: list[dict] = [{"type": "text", "text": req.user_prompt}]
        for page in req.page_images():
            if page.label:
                parts.append({"type": "text", "text": page.label})
            data_url = f"data:{page.mime_type};base64,{base64.b64encode(page.image).decode('ascii')}"
            parts.append({"type": "image_url", "image_url": {"url": data_url, "detail": "high"}})
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": req.system_prompt},
                    {"role": "user", "content": parts},
                ],
                **{self.token_param: req.max_output_tokens},
            )
        except openai.AuthenticationError as exc:
            raise ProviderError(f"{self.label}: authentication failed - check the API key") from exc
        except openai.NotFoundError as exc:
            raise ProviderError(f"{self.label}: model/deployment '{model}' not found") from exc
        except openai.RateLimitError as exc:
            raise ProviderError(f"{self.label}: rate limited, try again shortly") from exc
        except openai.APIStatusError as exc:
            raise ProviderError(f"{self.label}: API error {exc.status_code}: {exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(f"{self.label}: connection error: {exc}") from exc

        if not response.choices:
            raise ProviderError(f"{self.label}: empty response")
        choice = response.choices[0]
        warnings = []
        if choice.finish_reason == "length":
            warnings.append("output hit the max token limit; transcription is truncated")
        if choice.finish_reason == "content_filter":
            raise ProviderError(f"{self.label}: response blocked by the content filter")
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            raise ProviderError(f"{self.label}: the model declined this page ({refusal})")
        usage = response.usage
        return TranscriptionResult(
            text=(choice.message.content or "").strip(),
            provider=self.id,
            model=response.model or model,
            latency_ms=0,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            stop_reason=choice.finish_reason,
            warnings=warnings,
        )


class OpenAIProvider(_OpenAIChatBase):
    id = "openai"
    label = "OpenAI"
    docs_url = "https://platform.openai.com/api-keys"
    can_list_models = True
    env_vars = [
        ("OPENAI_API_KEY", True, "API key from platform.openai.com"),
        ("OPENAI_MODEL", False, "default model, e.g. gpt-6.2-sol"),
        ("OPENAI_BASE_URL", False, "only for proxies / gateways"),
    ]
    suggested_models = catalog.recommended("openai")

    @property
    def default_model(self) -> str | None:
        return self.settings.openai_model

    def missing_settings(self) -> list[str]:
        return [] if self.settings.openai_api_key else ["OPENAI_API_KEY"]

    def _make_client(self):
        return openai.AsyncOpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            timeout=self.settings.llm_timeout_s,
        )


class AzureOpenAIProvider(_OpenAIChatBase):
    id = "azure_openai"
    label = "Azure OpenAI"
    model_hint = "Azure deployment name"
    docs_url = "https://learn.microsoft.com/azure/ai-services/openai/"
    env_vars = [
        ("AZURE_OPENAI_API_KEY", True, "key of your Azure OpenAI resource"),
        ("AZURE_OPENAI_ENDPOINT", True, "https://<resource>.openai.azure.com"),
        ("AZURE_OPENAI_DEPLOYMENT", False, "name of a vision-capable deployment (gpt-6.2-sol, gpt-6.1-terra ...)"),
        ("AZURE_OPENAI_API_VERSION", False, "default 2024-10-21"),
    ]

    @property
    def default_model(self) -> str | None:
        return self.settings.azure_openai_deployment

    @property
    def suggested_models(self) -> list[str]:  # type: ignore[override]
        return [self.settings.azure_openai_deployment] if self.settings.azure_openai_deployment else []

    def missing_settings(self) -> list[str]:
        missing = []
        if not self.settings.azure_openai_api_key:
            missing.append("AZURE_OPENAI_API_KEY")
        if not self.settings.azure_openai_endpoint:
            missing.append("AZURE_OPENAI_ENDPOINT")
        return missing

    def _make_client(self):
        return openai.AsyncAzureOpenAI(
            api_key=self.settings.azure_openai_api_key,
            azure_endpoint=self.settings.azure_openai_endpoint,
            api_version=self.settings.azure_openai_api_version,
            timeout=self.settings.llm_timeout_s,
        )


class OpenAICompatibleProvider(_OpenAIChatBase):
    id = "openai_compatible"
    label = "OpenAI-compatible (Ollama, vLLM, ...)"
    docs_url = "https://github.com/ollama/ollama/blob/main/docs/openai.md"
    can_list_models = True
    env_vars = [
        ("OPENAI_COMPATIBLE_BASE_URL", True, "e.g. http://localhost:11434/v1 for Ollama"),
        ("OPENAI_COMPATIBLE_MODEL", False, "a vision model, e.g. qwen2.5vl:7b or llama3.2-vision"),
        ("OPENAI_COMPATIBLE_API_KEY", False, "only if the server needs one (OpenRouter does)"),
    ]
    token_param = "max_tokens"

    @property
    def default_model(self) -> str | None:
        return self.settings.openai_compatible_model

    @property
    def suggested_models(self) -> list[str]:  # type: ignore[override]
        return [self.settings.openai_compatible_model] if self.settings.openai_compatible_model else []

    def missing_settings(self) -> list[str]:
        return [] if self.settings.openai_compatible_base_url else ["OPENAI_COMPATIBLE_BASE_URL"]

    def _make_client(self):
        return openai.AsyncOpenAI(
            # Local servers usually ignore the key, but the SDK requires one.
            api_key=self.settings.openai_compatible_api_key or "not-needed",
            base_url=self.settings.openai_compatible_base_url,
            timeout=self.settings.llm_timeout_s,
        )
