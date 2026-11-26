"""LLM providers that can transcribe a page image."""

from __future__ import annotations

from ..config import Settings
from .anthropic_provider import AnthropicFoundryProvider, AnthropicProvider
from .base import (
    Provider,
    ProviderError,
    ProviderNotConfigured,
    TranscriptionRequest,
    TranscriptionResult,
)
from .gemini_provider import GeminiProvider
from .openai_provider import AzureOpenAIProvider, OpenAICompatibleProvider, OpenAIProvider
from .prompts import PROMPT_MODES, USER_PROMPT, resolve_prompt

PROVIDER_CLASSES: list[type[Provider]] = [
    AnthropicProvider,
    OpenAIProvider,
    AzureOpenAIProvider,
    AnthropicFoundryProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
]


def build_providers(settings: Settings) -> dict[str, Provider]:
    return {cls.id: cls(settings) for cls in PROVIDER_CLASSES}


__all__ = [
    "PROMPT_MODES",
    "USER_PROMPT",
    "Provider",
    "ProviderError",
    "ProviderNotConfigured",
    "TranscriptionRequest",
    "TranscriptionResult",
    "build_providers",
    "resolve_prompt",
]
