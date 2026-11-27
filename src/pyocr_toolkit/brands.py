"""Logos for LLM providers and OCR engines, shown next to their names in the UI.

Seeing a familiar logo is faster than reading "Azure AI Document Intelligence"
in a dropdown, so every provider and engine names a *brand* here. A brand is
either a bundled SVG (``static/logos/<file>``, tinted white on the brand
colour with a CSS mask, so it works in light and dark themes) or, when no
freely licensed logo exists, a short lettered badge on the brand colour.

The SVGs come from Simple Icons (CC0); see ``static/logos/README.md``. Several
companies (OpenAI, Microsoft, AWS) asked Simple Icons to remove their marks, so
those get a lettered badge rather than a hand-made copy of their logo.
Everything is served locally: the app never loads images from third parties.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Brand:
    name: str
    color: str  # tile background, "#rrggbb"
    icon: str | None = None  # file in static/logos/, or None for a lettered badge
    text: str = ""  # badge letters when there is no icon
    fg: str = "#ffffff"  # glyph / letter colour


BRANDS: dict[str, Brand] = {
    # LLM providers
    "claude": Brand("Claude", "#D97757", icon="claude.svg"),
    "openai": Brand("OpenAI", "#10A37F", text="AI"),
    "azure": Brand("Microsoft Azure", "#0078D4", text="Az"),
    "gemini": Brand("Google Gemini", "#8E75B2", icon="googlegemini.svg"),
    "openai_compatible": Brand("OpenAI-compatible server", "#475569", text="API"),
    # OCR engines
    "tesseract": Brand("Tesseract", "#1E5BA8", text="Tes"),
    "rapidocr": Brand("RapidOCR", "#E85D24", text="R"),
    "easyocr": Brand("EasyOCR", "#2E9E6A", text="E"),
    "paddleocr": Brand("PaddleOCR", "#0062B0", icon="paddlepaddle.svg"),
    "doctr": Brand("docTR (Mindee)", "#FD3246", text="dTR"),
    "google_cloud": Brand("Google Cloud", "#4285F4", icon="googlecloud.svg"),
    "aws": Brand("Amazon Web Services", "#232F3E", text="aws", fg="#FF9900"),
    "mistral": Brand("Mistral AI", "#FA520F", icon="mistralai.svg"),
}

_FALLBACK = Brand("", "#64748B", text="?")


def brand(key: str | None) -> Brand:
    return BRANDS.get(key or "", _FALLBACK)


def brands_json() -> dict[str, dict]:
    """All brands, for ``/api/config`` (the browser draws logos from this)."""
    return {k: asdict(b) for k, b in BRANDS.items()}
