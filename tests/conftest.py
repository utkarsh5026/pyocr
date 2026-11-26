from __future__ import annotations

import shutil

import pymupdf
import pytest

HAS_TESSERACT = shutil.which("tesseract") is not None

TEXT_PAGE = "Invoice 2024-117\nThe quick brown fox jumps over the lazy dog.\nTotal due: $1,250.00"
SCAN_PAGE = "Scanned receipt\nThe quick brown fox"


def _make_pdf() -> bytes:
    """Page 1 has a real text layer; page 2 is an image of text (a 'scan')."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), TEXT_PAGE, fontsize=14)

    # render some text to an image, then place only the image on page 2
    src = pymupdf.open()
    sp = src.new_page(width=400, height=200)
    sp.insert_text((20, 60), SCAN_PAGE, fontsize=20)
    png = sp.get_pixmap(dpi=200).tobytes("png")
    scan = doc.new_page(width=595, height=842)
    scan.insert_image(pymupdf.Rect(72, 72, 472, 272), stream=png)
    return doc.tobytes()


def _make_png() -> bytes:
    src = pymupdf.open()
    sp = src.new_page(width=400, height=150)
    sp.insert_text((20, 60), "Hello image world", fontsize=22)
    return sp.get_pixmap(dpi=150).tobytes("png")


@pytest.fixture(scope="session")
def sample_pdf() -> bytes:
    return _make_pdf()


@pytest.fixture(scope="session")
def sample_png() -> bytes:
    return _make_png()
