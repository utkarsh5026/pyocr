"""Provider-neutral interface for "transcribe this page image" calls."""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field

from ..config import env_status
from . import catalog


class ProviderError(Exception):
    """A provider call failed (bad credentials, refusal, network, ...)."""


class ProviderNotConfigured(ProviderError):
    pass


@dataclass
class TranscriptionRequest:
    image: bytes
    mime_type: str  # "image/png" or "image/jpeg"
    system_prompt: str
    user_prompt: str
    model: str | None = None  # None -> provider default
    max_output_tokens: int = 16000


@dataclass
class TranscriptionResult:
    text: str
    provider: str
    model: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


class Provider(abc.ABC):
    #: stable identifier used in URLs / cache keys
    id: str
    #: human readable name for the UI
    label: str
    #: example model names shown as suggestions in the UI
    suggested_models: list[str] = []
    #: what the "model" field means for this provider
    model_hint: str = "Model name"
    #: (ENV_VAR, required, description) - shown on the Providers page
    env_vars: list[tuple[str, bool, str]] = []
    #: where to get credentials
    docs_url: str = ""
    #: whether :meth:`list_models` asks the provider's API (False: deployments you name yourself)
    can_list_models: bool = False

    #: key into ``brands.BRANDS`` for the logo shown in the UI
    brand: str = ""

    def env_status(self) -> list[dict]:
        """Which settings are set (never the values themselves)."""
        return env_status(getattr(self, "settings", None), self.env_vars)

    @property
    @abc.abstractmethod
    def default_model(self) -> str | None: ...

    @abc.abstractmethod
    def missing_settings(self) -> list[str]:
        """Environment variables that must be set before the provider works."""

    @property
    def configured(self) -> bool:
        return not self.missing_settings()

    def reset_client(self) -> None:
        """Forget the cached SDK client so the next call picks up changed settings."""
        if hasattr(self, "_client"):
            self._client = None

    async def list_models(self) -> list[str] | None:
        """Model ids the provider says this key can use, or ``None`` if it can't list them.

        Azure/Foundry deployments aren't listable with a data-plane key, so those return ``None``.
        """
        return None

    async def transcribe(self, req: TranscriptionRequest) -> TranscriptionResult:
        missing = self.missing_settings()
        if missing:
            raise ProviderNotConfigured(f"{self.label} is not configured; set {', '.join(missing)}")
        model = (req.model or self.default_model or "").strip()
        if not model:
            raise ProviderError(f"no model given for {self.label}")
        t0 = time.perf_counter()
        result = await self._transcribe(req, model)
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result

    @abc.abstractmethod
    async def _transcribe(self, req: TranscriptionRequest, model: str) -> TranscriptionResult: ...

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "configured": self.configured,
            "missing": self.missing_settings(),
            "default_model": self.default_model,
            "suggested_models": self.suggested_models,
            "model_options": catalog.model_options(self.id, self.suggested_models),
            "model_hint": self.model_hint,
            "env": self.env_status(),
            "docs_url": self.docs_url,
            "brand": self.brand,
            "can_list_models": self.can_list_models,
        }
