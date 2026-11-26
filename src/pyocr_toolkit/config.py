"""Runtime configuration.

Every setting can be supplied through environment variables (or a ``.env``
file in the working directory). Provider credentials are *only* read on the
server side; the browser never sees an API key.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage / rendering -------------------------------------------------
    data_dir: Path = Path("data")
    max_upload_mb: int = 200
    # DPI used for the page preview shown in the browser.
    preview_dpi: int = 110
    # DPI used for the image sent to the LLM and for Tesseract OCR.
    llm_dpi: int = 200
    ocr_dpi: int = 300
    # Longest edge (px) of the image sent to an LLM. Larger images get
    # downscaled; most vision models resize internally anyway, so sending
    # a huge bitmap only costs upload time.
    llm_max_edge_px: int = 2400
    tesseract_language: str = "eng"
    # How many LLM calls may run at once when transcribing many pages.
    llm_concurrency: int = 4
    llm_timeout_s: float = 300.0
    # Rupees per US dollar, used to show LLM costs in ₹ next to the USD list
    # prices from the model catalog. Not fetched live (no outbound calls besides
    # the chosen provider); ~92.5 on 2026-11-26. Set it to today's rate.
    usd_inr_rate: float = 92.5

    # --- extra OCR engines ---------------------------------------------------
    # Longest edge (px) of the page image given to the non-Tesseract OCR
    # engines (local models and cloud APIs). OCR_DPI is lowered to fit.
    ocr_max_edge_px: int = 4000
    ocr_timeout_s: float = 120.0
    # Run local deep-learning engines (EasyOCR, PaddleOCR, docTR) on a GPU.
    ocr_gpu: bool = False
    # Comma-separated EasyOCR language codes, e.g. "en,de".
    easyocr_languages: str = "en"
    # PaddleOCR language code, e.g. "en", "ch", "german", "japan".
    paddleocr_language: str = "en"

    # Google Cloud Vision (DOCUMENT_TEXT_DETECTION)
    google_vision_api_key: str | None = None
    # Optional comma-separated language hints ("en,de"); empty = auto-detect.
    google_vision_language_hints: str = ""

    # Azure AI Document Intelligence (prebuilt-read model)
    azure_document_intelligence_endpoint: str | None = None
    azure_document_intelligence_key: str | None = None

    # AWS Textract (DetectDocumentText). Credentials can also come from the
    # usual AWS chain (~/.aws/credentials, AWS_PROFILE, instance roles).
    aws_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    # Mistral OCR API
    mistral_api_key: str | None = None
    mistral_ocr_model: str = "mistral-ocr-latest"

    # --- OpenAI --------------------------------------------------------------
    openai_api_key: str | None = None
    openai_model: str = "gpt-6.2-sol"
    openai_base_url: str | None = None

    # --- Azure OpenAI --------------------------------------------------------
    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployment: str | None = None

    # --- Anthropic (Claude API) ----------------------------------------------
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5-5"
    # Effort controls how much the model thinks before answering
    # (low | medium | high | xhigh | max). Transcription is mostly perception,
    # so "medium" is a good balance; raise it for dense/handwritten pages.
    anthropic_effort: str = "medium"

    # --- Claude on Microsoft Foundry (Azure) ---------------------------------
    anthropic_foundry_api_key: str | None = None
    anthropic_foundry_resource: str | None = None
    anthropic_foundry_base_url: str | None = None
    anthropic_foundry_model: str = "claude-opus-5-5"

    # --- Google Gemini -------------------------------------------------------
    gemini_api_key: str | None = None
    google_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"

    # --- Any OpenAI-compatible server (Ollama, vLLM, LM Studio, OpenRouter) --
    openai_compatible_base_url: str | None = None
    openai_compatible_api_key: str | None = None
    openai_compatible_model: str | None = None

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"


def env_status(settings: BaseSettings | None, env_vars: list[tuple[str, bool, str]]) -> list[dict]:
    """Which of ``env_vars`` are set: "set", "default" or "missing". Never returns values.

    Shared by LLM providers and OCR engines so the Providers page can show what
    is configured without a secret ever reaching the browser.
    """
    fields = type(settings).model_fields if settings is not None else {}
    out = []
    for name, required, desc in env_vars:
        attr = name.lower()
        value = getattr(settings, attr, None)
        default = fields[attr].default if attr in fields else None
        # "default" = not set by the user but a built-in default applies
        state = "missing" if not value else "default" if value == default else "set"
        out.append({"name": name, "required": required, "description": desc,
                    "set": state == "set", "state": state,
                    "default": default if isinstance(default, (str, int, float)) else None})
    return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
