"""Other Python PDF libraries, as alternative readers of a PDF's embedded text layer.

PyMuPDF is what the app uses for ``native`` text, but it is not the only way to
read a PDF's text layer, and the libraries disagree: reading order, spacing,
ligatures, hyphenation and odd font encodings all come out differently. If a
pipeline downstream uses pypdf or pdfplumber, the question "does *this* PDF
extract cleanly?" has to be asked of *that* library. So each one is offered as
its own source, ``native:<library>``, next to the PyMuPDF one:

* ``native``             - PyMuPDF (``native:pymupdf`` is accepted as an alias)
* ``native:pypdf``       - pypdf, pure Python
* ``native:pdfplumber``  - pdfplumber (built on pdfminer.six), with word boxes
* ``native:pdfminer``    - pdfminer.six's ``extract_text``
* ``native:pypdfium2``   - PDFium, the engine in Chrome

None of them is required. A library that isn't installed is reported (with the
``pip install`` that fixes it) instead of listed as a source. They only read PDF
files; images and other formats have no PDF text layer to read.

They read ``source.pdf`` from disk, never the shared PyMuPDF handle, so they don't
need the per-document PyMuPDF lock. Each library gets its own lock instead
(PDFium in particular is not thread-safe). Results are cached per page under
``native/<library>/<page>.json`` together with the library version, so a repeated
compare is instant and an upgraded library re-extracts.
"""

from __future__ import annotations

import abc
import importlib.metadata
import importlib.util
import threading
import time
from typing import TYPE_CHECKING, Any

from .documents import DocumentError, OCRUnavailableError, TextLayer, Word

if TYPE_CHECKING:
    from .documents import OpenDocument

#: the library behind the plain ``native`` source
DEFAULT_TEXT_LIBRARY = "pymupdf"


def native_library_of(source: str) -> str | None:
    """``native`` -> ``pymupdf``, ``native:<id>`` -> ``<id>``, anything else -> ``None``."""
    if source == "native":
        return DEFAULT_TEXT_LIBRARY
    if source.startswith("native:"):
        return source[7:] or DEFAULT_TEXT_LIBRARY
    return None


def native_source_id(library: str) -> str:
    return "native" if library == DEFAULT_TEXT_LIBRARY else f"native:{library}"


class TextLibrary(abc.ABC):
    #: stable id, used in source ids (``native:<id>``), cache folders and URLs
    id: str
    label: str
    description: str = ""
    docs_url: str = ""
    #: module to import, and the distribution whose version is reported
    module: str = ""
    distribution: str = ""
    #: False when the library gives text only (no word boxes to highlight)
    has_boxes: bool = False

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def source_id(self) -> str:
        return native_source_id(self.id)

    def install_hint(self) -> str:
        return f"pip install {self.distribution}"

    def version(self) -> str | None:
        try:
            return importlib.metadata.version(self.distribution)
        except importlib.metadata.PackageNotFoundError:
            return None

    def problem(self) -> str | None:
        """Why the library can't run here (and what to do), or ``None`` when it's ready."""
        if importlib.util.find_spec(self.module) is None:
            return (f"{self.label} is not installed: run {self.install_hint()} "
                    f'(or pip install "pyocr-toolkit[pdf]" for all PDF libraries)')
        return None

    @property
    def available(self) -> bool:
        return self.problem() is None

    def describe(self) -> dict[str, Any]:
        problem = self.problem()
        return {"id": self.id, "source_id": self.source_id, "label": self.label, "description": self.description,
                "docs_url": self.docs_url, "available": problem is None, "problem": problem,
                "install": self.install_hint(), "version": self.version(), "has_boxes": self.has_boxes}

    # ------------------------------------------------------------ running
    def extract(self, doc: OpenDocument, page: int, force: bool = False) -> TextLayer:
        """Text of one page of ``doc`` as this library reads it (cached on disk unless ``force``)."""
        if not doc.meta.is_pdf:
            raise DocumentError(f"{self.label} reads PDF files only; '{doc.meta.filename}' is not a PDF")
        if not 0 <= page < doc.meta.page_count:
            raise DocumentError(f"page {page} out of range (document has {doc.meta.page_count} pages)")
        version = self.version()
        if not force and (cached := doc.cached_native_layer(page, self.id)) is not None:
            if cached.meta.get("version") == version:
                return cached
        if problem := self.problem():
            raise OCRUnavailableError(problem)
        t0 = time.perf_counter()
        try:
            with self._lock:
                text, words = self.read_page(str(doc.source_path), page)
        except DocumentError:
            raise
        except Exception as exc:  # the libraries raise many types on malformed files
            raise DocumentError(f"{self.label} could not read page {page + 1}: {type(exc).__name__}: {exc}") from exc
        layer = TextLayer(self.source_id, text or "", words, elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                          meta={"library": self.id, "label": self.label, "version": version})
        doc.save_native_layer(page, self.id, layer)
        return layer

    @abc.abstractmethod
    def read_page(self, path: str, page: int) -> tuple[str, list[Word]]:
        """(text, word boxes in PDF points with a top-left origin) of one 0-based page."""


class PyPDFLibrary(TextLibrary):
    id = "pypdf"
    label = "pypdf"
    description = "Pure-Python PDF library (no compiled code). Widely used by LangChain/LlamaIndex PDF loaders."
    docs_url = "https://pypdf.readthedocs.io/"
    module = distribution = "pypdf"

    def read_page(self, path: str, page: int) -> tuple[str, list[Word]]:
        from pypdf import PdfReader

        return PdfReader(path).pages[page].extract_text() or "", []


class PdfPlumberLibrary(TextLibrary):
    id = "pdfplumber"
    label = "pdfplumber"
    description = "Built on pdfminer.six; good at layout and tables, gives word positions."
    docs_url = "https://github.com/jsvine/pdfplumber"
    module = distribution = "pdfplumber"
    has_boxes = True

    def read_page(self, path: str, page: int) -> tuple[str, list[Word]]:
        import pdfplumber

        with pdfplumber.open(path, pages=[page + 1]) as pdf:
            p = pdf.pages[0]
            text = p.extract_text() or ""
            raw = p.extract_words()
        words: list[Word] = []
        line, prev = 0, None
        for w in raw:  # already in reading order; a new line starts when the word moves down
            if prev is not None and w["top"] - prev["top"] > 0.5 * (prev["bottom"] - prev["top"]):
                line += 1
            words.append(Word(w["x0"], w["top"], w["x1"], w["bottom"], w["text"], 0, line))
            prev = w
        return text, words


class PdfMinerLibrary(TextLibrary):
    id = "pdfminer"
    label = "pdfminer.six"
    description = "Pure-Python layout analysis; the text engine underneath pdfplumber, with its own defaults."
    docs_url = "https://pdfminersix.readthedocs.io/"
    module, distribution = "pdfminer", "pdfminer.six"

    def read_page(self, path: str, page: int) -> tuple[str, list[Word]]:
        from pdfminer.high_level import extract_text

        return extract_text(path, page_numbers=[page]), []


class PyPdfium2Library(TextLibrary):
    id = "pypdfium2"
    label = "pypdfium2 (PDFium)"
    description = "Python bindings to PDFium, the PDF engine in Chrome. Fast; text as Chrome would copy it."
    docs_url = "https://pypdfium2.readthedocs.io/"
    module = distribution = "pypdfium2"

    def read_page(self, path: str, page: int) -> tuple[str, list[Word]]:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(path)
        try:
            p = pdf[page]
            tp = p.get_textpage()
            try:
                # PDFium uses \r\n line ends
                return tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n"), []
            finally:
                tp.close()
                p.close()
        finally:
            pdf.close()


TEXT_LIBRARY_CLASSES: list[type[TextLibrary]] = [PyPDFLibrary, PdfPlumberLibrary, PdfMinerLibrary, PyPdfium2Library]


def build_text_libraries() -> dict[str, TextLibrary]:
    return {cls.id: cls() for cls in TEXT_LIBRARY_CLASSES}


__all__ = ["DEFAULT_TEXT_LIBRARY", "TEXT_LIBRARY_CLASSES", "TextLibrary", "build_text_libraries",
           "native_library_of", "native_source_id"]
