"""Engine-neutral interface for "OCR this page" plus shared geometry helpers.

Every engine turns a page image into a :class:`~pyocr_toolkit.documents.TextLayer`:
plain text in reading order plus word boxes in PDF points (the same shape as
the native text layer). That way search highlights, compare and export work
the same for every engine.

Engines differ in what they return. Some give words grouped in blocks and
lines (Tesseract, docTR, Google Vision, Textract). Others give unordered
*line* boxes (RapidOCR, EasyOCR, PaddleOCR). Mistral gives Markdown text and
no boxes. The helpers here rebuild reading order from line boxes and split a
line box into approximate word boxes, so the rest of the app never has to
care which kind it got.

Engines run synchronously (in a worker thread when called from async code)
and cache their result per page under ``ocr/<engine>/<page>.json``.
"""

from __future__ import annotations

import abc
import importlib.util
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pymupdf

from ..config import Settings, env_status
from ..documents import DEFAULT_OCR_ENGINE, OCRUnavailableError, RenderedImage, TextLayer, Word

if TYPE_CHECKING:
    from ..documents import OpenDocument

#: A detected text item in pixel coordinates: (x0, y0, x1, y1, text).
Box = tuple[float, float, float, float, str]


@dataclass
class OCRResult:
    """What an engine read from one image. Word boxes are in **image pixels**."""

    words: list[Word]
    #: the engine's own reading-order text; ``None`` -> rebuilt from ``words``
    text: str | None = None
    model: str | None = None
    warnings: list[str] = field(default_factory=list)


class OCREngine(abc.ABC):
    #: stable id, used in source ids (``ocr:<id>``), cache folders and URLs
    id: str
    label: str
    #: "local" runs on this machine; "cloud" sends the page image to a service
    kind: str = "local"
    #: key into ``brands.BRANDS``
    brand: str = ""
    description: str = ""
    docs_url: str = ""
    #: (ENV_VAR, required, description), shown on the Providers page
    env_vars: list[tuple[str, bool, str]] = []
    #: importable modules this engine needs; any one of each inner tuple will do
    requires: list[tuple[str, ...]] = []
    #: ``pip install "pyocr-toolkit[<extra>]"`` installs ``requires``
    pip_extra: str | None = None
    #: False when the engine returns text only (no word boxes to highlight)
    has_boxes: bool = True
    #: pages processed in parallel by a background job
    concurrency: int = 1
    #: image format handed to :meth:`recognize`: "png" or "raw" (RGB samples)
    image_format: str = "png"
    #: re-encode as JPEG above this size (cloud upload limits)
    max_image_bytes: int | None = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Local engines hold a model in memory; loading it is slow and the
        # libraries are not documented as thread-safe, so one call at a time.
        self._lock = threading.Lock()
        self._model: Any = None

    # ------------------------------------------------------------ status
    @property
    def source_id(self) -> str:
        return "ocr" if self.id == DEFAULT_OCR_ENGINE else f"ocr:{self.id}"

    def env_status(self) -> list[dict]:
        """Which settings are set (never the values themselves)."""
        return env_status(self.settings, self.env_vars)

    def reset_client(self) -> None:
        """Forget the loaded model / HTTP client so changed settings (keys, languages) apply."""
        with self._lock:
            self._model = None
            if getattr(self, "_http", None) is not None:
                self._http = None

    def missing_modules(self) -> list[str]:
        return [alts[0] for alts in self.requires if not any(importlib.util.find_spec(m) for m in alts)]

    def missing_settings(self) -> list[str]:
        """Required environment variables that are not set."""
        return []

    def install_hint(self) -> str:
        return f'pip install "pyocr-toolkit[{self.pip_extra}]"' if self.pip_extra else ""

    def problem(self) -> str | None:
        """Why the engine can't run here (what to do about it), or ``None`` when it's ready."""
        if missing := self.missing_modules():
            return f"{self.label} is not installed ({', '.join(missing)} missing): run {self.install_hint()}"
        if missing := self.missing_settings():
            return f"{self.label} is not configured: set {', '.join(missing)}"
        return None

    @property
    def available(self) -> bool:
        return self.problem() is None

    def settings_summary(self) -> str:
        """Short description of the settings in effect (language, model, ...)."""
        return ""

    def describe(self) -> dict[str, Any]:
        problem = self.problem()
        return {
            "id": self.id,
            "source_id": self.source_id,
            "label": self.label,
            "kind": self.kind,
            "brand": self.brand,
            "description": self.description,
            "docs_url": self.docs_url,
            "available": problem is None,
            "problem": problem,
            "installed": not self.missing_modules(),
            "install": self.install_hint(),
            "env": self.env_status(),
            "missing": self.missing_settings(),
            "has_boxes": self.has_boxes,
            "summary": self.settings_summary(),
        }

    # ------------------------------------------------------------ running
    def extract(self, doc: OpenDocument, page: int, force: bool = False) -> TextLayer:
        """OCR one page of ``doc`` (cached on disk unless ``force``)."""
        if not force and (cached := doc.cached_ocr_text(page, self.id)) is not None:
            return cached
        if problem := self.problem():
            raise OCRUnavailableError(problem)
        s = self.settings
        image = doc.render_image(page, s.ocr_dpi, self.image_format, s.ocr_max_edge_px)
        if self.max_image_bytes and image.mime == "image/png" and len(image.data) > self.max_image_bytes:
            image = doc.render_image(page, s.ocr_dpi, "jpeg", s.ocr_max_edge_px)
        t0 = time.perf_counter()
        result = self.recognize(image)
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        sx, sy = image.page_width / image.width_px, image.page_height / image.height_px
        words = [Word(w.x0 * sx, w.y0 * sy, w.x1 * sx, w.y1 * sy, w.text, w.block, w.line) for w in result.words]
        text = result.text if result.text is not None else words_to_text(words)
        meta = {"engine": self.id, "label": self.label, "model": result.model, "warnings": result.warnings,
                "settings": self.settings_summary(), "image_px": [image.width_px, image.height_px]}
        layer = TextLayer(self.source_id, text, words, elapsed_ms=elapsed, meta=meta)
        doc.save_ocr_layer(page, self.id, layer)
        return layer

    @abc.abstractmethod
    def recognize(self, image: RenderedImage) -> OCRResult:
        """Read text from one image. Boxes in the result are in image pixels."""

    def test(self) -> dict[str, Any]:
        """OCR a small generated image and report what came back (Providers page)."""
        if problem := self.problem():
            raise OCRUnavailableError(problem)
        expected = "pyocr test 4821"
        tmp = pymupdf.open()
        page = tmp.new_page(width=300, height=80)
        page.insert_text((20, 50), expected, fontsize=22)
        pix = page.get_pixmap(dpi=200, alpha=False, colorspace=pymupdf.csRGB)
        data = bytes(pix.samples) if self.image_format == "raw" else pix.tobytes("png")
        image = RenderedImage(data, "raw" if self.image_format == "raw" else "image/png",
                              pix.width, pix.height, page.rect.width, page.rect.height)
        t0 = time.perf_counter()
        result = self.recognize(image)
        text = result.text if result.text is not None else words_to_text(result.words)
        return {"ok": expected in " ".join(text.split()), "expected": expected, "text": text.strip(),
                "model": result.model, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "warnings": result.warnings}

    def _with_model(self, factory):
        """Load the model once (thread-safely) and return it."""
        if self._model is None:
            self._model = factory()
        return self._model


# ---------------------------------------------------------------- geometry
def rect_of(points: Iterable[Sequence[float]]) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box of a polygon given as ``[[x, y], ...]``."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def pairs(flat: Sequence[float]) -> list[tuple[float, float]]:
    """``[x1, y1, x2, y2, ...]`` -> ``[(x1, y1), (x2, y2), ...]``."""
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def split_line(x0: float, y0: float, x1: float, y1: float, text: str, block: int, line: int) -> list[Word]:
    """Split a line box into word boxes, sharing the width by character count.

    Engines like EasyOCR only box whole lines; an approximate per-word box is
    still good enough to highlight a search hit in the right place.
    """
    tokens = text.split()
    if not tokens:
        return []
    # count one space between words so the boxes don't touch
    total = sum(len(t) for t in tokens) + len(tokens) - 1
    unit = (x1 - x0) / max(total, 1)
    words, pos = [], 0
    for t in tokens:
        words.append(Word(x0 + pos * unit, y0, x0 + (pos + len(t)) * unit, y1, t, block, line))
        pos += len(t) + 1
    return words


def words_from_boxes(boxes: Iterable[Box]) -> list[Word]:
    """Order unordered line boxes into reading order and split them into words.

    Boxes are grouped into rows when they overlap vertically by at least half
    the smaller box's height, then each row is read left to right. Every row
    becomes one line of block 0. This is a simple single-column heuristic:
    multi-column pages read across columns, as with most line-level engines.
    """
    items = sorted((b for b in boxes if b[4].strip()), key=lambda b: (b[1] + b[3]) / 2)
    rows: list[list[Box]] = []
    for b in items:
        if rows:
            last = rows[-1]
            top, bottom = min(r[1] for r in last), max(r[3] for r in last)
            overlap = min(bottom, b[3]) - max(top, b[1])
            if overlap >= 0.5 * min(bottom - top, b[3] - b[1]):
                last.append(b)
                continue
        rows.append([b])
    words: list[Word] = []
    for i, row in enumerate(rows):
        for b in sorted(row, key=lambda r: r[0]):
            words.extend(split_line(*b[:4], b[4], block=0, line=i))
    return words


def words_to_text(words: Sequence[Word]) -> str:
    """Join words into text: spaces within a line, newlines between lines, a blank line between blocks."""
    out: list[str] = []
    prev: tuple[int, int] | None = None
    for w in words:
        if prev is not None:
            if w.block != prev[0]:
                out.append("\n\n")
            elif w.line != prev[1]:
                out.append("\n")
            else:
                out.append(" ")
        out.append(w.text)
        prev = (w.block, w.line)
    return "".join(out) + ("\n" if out else "")
