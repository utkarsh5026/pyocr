"""LLM providers that can transcribe a page image."""

from __future__ import annotations

from ..config import Settings
from . import catalog
from .anthropic_provider import AnthropicFoundryProvider, AnthropicProvider
from .base import (
    PageImage,
    Provider,
    ProviderError,
    ProviderNotConfigured,
    TranscriptionRequest,
    TranscriptionResult,
)
from .gemini_provider import GeminiProvider
from .openai_provider import AzureOpenAIProvider, OpenAICompatibleProvider, OpenAIProvider
from .prompts import (
    BATCH_USER_PROMPT,
    MAX_PAGES_PER_REQUEST,
    PROMPT_MODES,
    USER_PROMPT,
    batch_rules,
    resolve_prompt,
    split_batch,
)

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
    "catalog",
    "BATCH_USER_PROMPT",
    "MAX_PAGES_PER_REQUEST",
    "PageImage",
    "batch_rules",
    "split_batch",
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
