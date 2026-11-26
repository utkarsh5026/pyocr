"""OCR engines that turn a page image into text + word boxes.

Tesseract (via PyMuPDF) is the default; the others are optional local
libraries or cloud services. See :mod:`pyocr_toolkit.ocr.base` for the
interface, and ``OCR_ENGINE_CLASSES`` for the order shown in the UI.
"""

from __future__ import annotations

from ..config import Settings
from .base import OCREngine, OCRResult, split_line, words_from_boxes, words_to_text
from .cloud import AzureDocumentIntelligenceEngine, GoogleVisionEngine, MistralOCREngine, TextractEngine
from .local import DocTREngine, EasyOCREngine, PaddleOCREngine, RapidOCREngine
from .tesseract import TesseractEngine

OCR_ENGINE_CLASSES: list[type[OCREngine]] = [
    TesseractEngine,
    RapidOCREngine,
    EasyOCREngine,
    PaddleOCREngine,
    DocTREngine,
    GoogleVisionEngine,
    AzureDocumentIntelligenceEngine,
    TextractEngine,
    MistralOCREngine,
]


def build_ocr_engines(settings: Settings) -> dict[str, OCREngine]:
    return {cls.id: cls(settings) for cls in OCR_ENGINE_CLASSES}


__all__ = ["OCR_ENGINE_CLASSES", "OCREngine", "OCRResult", "build_ocr_engines", "split_line",
           "words_from_boxes", "words_to_text"]
