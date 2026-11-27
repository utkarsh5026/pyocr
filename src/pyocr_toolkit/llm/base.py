"""Provider-neutral interface for "transcribe this page image" calls."""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field

from . import catalog
from .prompts import schema_instructions


class ProviderError(Exception):
    """A provider call failed (bad credentials, refusal, network, ...)."""


class ProviderNotConfigured(ProviderError):
    pass


@dataclass
class TranscriptionRequest:
    image: bytes  # empty = text only (e.g. drafting a schema without a sample page)
    mime_type: str  # "image/png" or "image/jpeg"
    system_prompt: str
    user_prompt: str
    model: str | None = None  # None -> provider default
    max_output_tokens: int = 16000
    #: structured extraction: a normalized JSON Schema the reply must follow (see ``extract.provider_schema``).
    #: Providers enforce it natively where they can, otherwise put it in the prompt (``system_with_schema``).
    output_schema: dict | None = None

    def system_with_schema(self) -> str:
        """The system prompt, plus the schema spelled out for models that can't enforce it natively."""
        return self.system_prompt + (schema_instructions(self.output_schema) if self.output_schema else "")


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

    def env_status(self) -> list[dict]:
        """Which settings are set (never the values themselves)."""
        settings = getattr(self, "settings", None)
        fields = type(settings).model_fields if settings is not None else {}
        out = []
        for name, required, desc in self.env_vars:
            attr = name.lower()
            value = getattr(settings, attr, None)
            default = fields[attr].default if attr in fields else None
            # "default" = not set by the user but a built-in default applies
            state = "missing" if not value else "default" if value == default else "set"
            out.append({"name": name, "required": required, "description": desc,
                        "set": state == "set", "state": state,
                        "default": default if isinstance(default, (str, int, float)) else None})
        return out

    @property
    @abc.abstractmethod
    def default_model(self) -> str | None: ...

    @abc.abstractmethod
    def missing_settings(self) -> list[str]:
        """Environment variables that must be set before the provider works."""

    @property
    def configured(self) -> bool:
        return not self.missing_settings()

    def schema_support(self, model: str | None) -> tuple[str, str]:
        """How a structured-extraction schema is handled for ``model``: (``ok`` | ``warn``, sentence for the UI)."""
        return "warn", f"{self.label} can't enforce a schema: it's given in the prompt, so check the fields"

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
            "can_list_models": self.can_list_models,
        }
