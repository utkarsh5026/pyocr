"""Claude, via the Anthropic API or via Microsoft Foundry (Azure)."""

from __future__ import annotations

import base64
import re

import anthropic

from ..config import Settings
from . import catalog
from .base import Provider, ProviderError, TranscriptionRequest, TranscriptionResult

# Models that accept the server-side refusal fallback (``fallbacks``). When a
# safety classifier declines a page (rare, but it happens with e.g. scanned
# lab reports or chemistry papers) the API transparently re-runs the request
# on a fallback model instead of returning nothing.
_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_VERSION_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:$|[-@])")


def _supports_effort(model: str) -> bool:
    """``output_config.effort`` exists on Opus/Sonnet 4.6+, Haiku 5.5+, Fable, Mythos.

    Older models reject the parameter, so we only send it where it's known to work.
    """
    m = _VERSION_RE.search(model)
    if not m:
        return False
    family, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    version = (major, minor)
    if family in ("fable", "mythos"):
        return True
    if family == "haiku":
        return version >= (5, 5)
    return version >= (4, 6)


class _ClaudeBase(Provider):
    suggested_models = catalog.recommended("anthropic") + ["claude-haiku-5-5"]
    supports_server_fallback = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: anthropic.AsyncAnthropic | None = None

    def _make_client(self) -> anthropic.AsyncAnthropic:
        raise NotImplementedError

    @property
    def client(self):
        if self._client is None:
            self._client = self._make_client()
        return self._client

    async def _transcribe(self, req: TranscriptionRequest, model: str) -> TranscriptionResult:
        params: dict = {
            "model": model,
            "max_tokens": req.max_output_tokens,
            "system": req.system_prompt,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": req.mime_type,
                            "data": base64.standard_b64encode(req.image).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": req.user_prompt},
                ],
            }],
        }
        if _supports_effort(model) and self.settings.anthropic_effort:
            params["output_config"] = {"effort": self.settings.anthropic_effort}
        if self.supports_server_fallback and model in _FALLBACK_MODELS:
            params["betas"] = [_FALLBACK_BETA]
            params["fallbacks"] = "default"

        try:
            # Streaming keeps long transcriptions (dense pages) clear of HTTP
            # timeouts; we only need the final message.
            async with self.client.beta.messages.stream(**params) as stream:
                message = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ProviderError(f"{self.label}: authentication failed - check the API key") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"{self.label}: model '{model}' not found") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"{self.label}: rate limited, try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"{self.label}: API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"{self.label}: connection error: {exc}") from exc

        warnings: list[str] = []
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderError(f"{self.label}: the model declined this page" + (f" ({category})" if category else ""))
        if message.stop_reason == "max_tokens":
            warnings.append("output hit the max token limit; transcription is truncated")
        for block in message.content:
            if block.type == "fallback":
                warnings.append(f"{block.from_.model} declined; answered by {block.to.model}")

        text = "".join(block.text for block in message.content if block.type == "text")
        return TranscriptionResult(
            text=text.strip(),
            provider=self.id,
            model=message.model,
            latency_ms=0,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
            warnings=warnings,
        )


class AnthropicProvider(_ClaudeBase):
    id = "anthropic"
    label = "Claude (Anthropic API)"
    brand = "claude"
    docs_url = "https://console.anthropic.com/settings/keys"
    can_list_models = True
    env_vars = [
        ("ANTHROPIC_API_KEY", True, "API key from the Claude Console"),
        ("ANTHROPIC_MODEL", False, "default model, e.g. claude-opus-5-5"),
        ("ANTHROPIC_EFFORT", False, "low | medium | high | xhigh | max (default medium)"),
    ]

    @property
    def default_model(self) -> str | None:
        return self.settings.anthropic_model

    def missing_settings(self) -> list[str]:
        return [] if self.settings.anthropic_api_key else ["ANTHROPIC_API_KEY"]

    def _make_client(self):
        return anthropic.AsyncAnthropic(
            api_key=self.settings.anthropic_api_key, timeout=self.settings.llm_timeout_s
        )

    async def list_models(self) -> list[str] | None:
        try:
            return [m.id async for m in self.client.models.list(limit=100)]
        except anthropic.APIError as exc:
            raise ProviderError(f"Could not list Claude models: {exc}") from exc


class AnthropicFoundryProvider(_ClaudeBase):
    """Claude models deployed in Microsoft Foundry (Azure AI)."""

    id = "azure_claude"
    label = "Claude on Azure (Microsoft Foundry)"
    brand = "claude"
    model_hint = "Foundry deployment name of the Claude model"
    docs_url = "https://learn.microsoft.com/azure/ai-foundry/"
    env_vars = [
        ("ANTHROPIC_FOUNDRY_API_KEY", True, "key of your Microsoft Foundry resource"),
        ("ANTHROPIC_FOUNDRY_RESOURCE", True, "resource name (or set ANTHROPIC_FOUNDRY_BASE_URL)"),
        ("ANTHROPIC_FOUNDRY_BASE_URL", False, "full endpoint URL, instead of the resource name"),
        ("ANTHROPIC_FOUNDRY_MODEL", False, "deployment name, e.g. claude-opus-5-5"),
    ]
    # Server-side fallbacks are a Claude API feature, not available on Foundry.
    supports_server_fallback = False

    @property
    def default_model(self) -> str | None:
        return self.settings.anthropic_foundry_model

    def missing_settings(self) -> list[str]:
        missing = []
        if not self.settings.anthropic_foundry_api_key:
            missing.append("ANTHROPIC_FOUNDRY_API_KEY")
        if not (self.settings.anthropic_foundry_resource or self.settings.anthropic_foundry_base_url):
            missing.append("ANTHROPIC_FOUNDRY_RESOURCE (or ANTHROPIC_FOUNDRY_BASE_URL)")
        return missing

    def _make_client(self):
        kwargs = {"api_key": self.settings.anthropic_foundry_api_key, "timeout": self.settings.llm_timeout_s}
        if self.settings.anthropic_foundry_base_url:
            kwargs["base_url"] = self.settings.anthropic_foundry_base_url
        else:
            kwargs["resource"] = self.settings.anthropic_foundry_resource
        return anthropic.AsyncAnthropicFoundry(**kwargs)
