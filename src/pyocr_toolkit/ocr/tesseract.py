"""Tesseract, through PyMuPDF's built-in OCR (the original engine).

Unlike the other engines it OCRs the PyMuPDF page directly (so PyMuPDF does
the rendering and gives real word boxes) and keeps its cache in the original
``ocr/<page>.json`` location, which keeps existing data folders readable.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

import pymupdf

from ..documents import DEFAULT_OCR_ENGINE, TESSERACT_LOCK, OCRUnavailableError, RenderedImage, TextLayer
from .base import OCREngine, OCRResult

if TYPE_CHECKING:
    from ..documents import OpenDocument


class TesseractEngine(OCREngine):
    id = DEFAULT_OCR_ENGINE
    label = "Tesseract OCR"
    kind = "local"
    brand = "tesseract"
    description = "Classic open-source OCR engine. Fast on clean printed text; needs the tesseract binary."
    docs_url = "https://tesseract-ocr.github.io/tessdoc/Installation.html"
    env_vars = [("TESSERACT_LANGUAGE", False, "Language pack(s), e.g. eng or eng+deu")]

    def missing_modules(self) -> list[str]:
        return [] if shutil.which("tesseract") else ["tesseract"]

    def problem(self) -> str | None:
        if not shutil.which("tesseract"):
            return ("Tesseract is not installed: apt install tesseract-ocr / brew install tesseract "
                    "(or use the Docker image)")
        return None

    def install_hint(self) -> str:
        return "apt install tesseract-ocr  ·  brew install tesseract"

    def settings_summary(self) -> str:
        return f"lang {self.settings.tesseract_language} · {self.settings.ocr_dpi} DPI"

    def extract(self, doc: OpenDocument, page: int, force: bool = False) -> TextLayer:
        layer = doc.ocr_text(page, dpi=self.settings.ocr_dpi, language=self.settings.tesseract_language, force=force)
        layer.meta.setdefault("label", self.label)
        return layer

    def recognize(self, image: RenderedImage) -> OCRResult:  # pragma: no cover - extract/test bypass it
        raise NotImplementedError

    def test(self) -> dict[str, Any]:
        if problem := self.problem():
            raise OCRUnavailableError(problem)
        import time

        expected = "pyocr test 4821"
        tmp = pymupdf.open()
        page = tmp.new_page(width=300, height=80)
        page.insert_text((20, 50), expected, fontsize=22)
        t0 = time.perf_counter()
        try:
            with TESSERACT_LOCK:
                tp = page.get_textpage_ocr(full=True, dpi=300, language=self.settings.tesseract_language)
        except RuntimeError as exc:
            raise OCRUnavailableError(f"Tesseract failed: {exc}") from exc
        text = page.get_text("text", textpage=tp).strip()
        return {"ok": expected in " ".join(text.split()), "expected": expected, "text": text, "model": None,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "warnings": []}
