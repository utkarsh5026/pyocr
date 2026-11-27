"""Document storage and everything that touches PyMuPDF.

A "document" is any file PyMuPDF can open: PDF, XPS, EPUB, CBZ, FB2, MOBI,
SVG and raster images (PNG, JPEG, TIFF, BMP, GIF, ...). Raster images open as
a single-page document (multi-frame TIFFs as several pages), so the rest of
the app never needs to special-case them.

On disk, each upload lives in its own folder::

    data/documents/<doc_id>/
        meta.json          # filename, page count, ...
        pages.json         # per-page facts (size, text-layer chars, ...); derived, rebuilt if missing
        source.<ext>       # the original bytes, untouched
        ocr/<page>.json    # cached Tesseract result (text + word boxes)
        ocr/<engine>/<page>.json  # cached result of another OCR engine (EasyOCR, ...)
        llm/<page>/<key>.json   # cached LLM transcriptions

Page numbers are **0-based** everywhere in the Python/HTTP API; the UI adds 1
when displaying them.

PyMuPDF objects are not thread-safe, and FastAPI runs sync endpoints in a
thread pool, so every access to an open ``pymupdf.Document`` goes through a
per-document lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

# Extensions PyMuPDF understands. Used to give the file a proper suffix so
# PyMuPDF picks the right parser, and to reject obviously wrong uploads early.
SUPPORTED_EXTENSIONS = {
    ".pdf", ".xps", ".oxps", ".epub", ".cbz", ".cbr", ".fb2", ".mobi", ".svg", ".txt",
    ".png", ".jpg", ".jpeg", ".jpx", ".jp2", ".bmp", ".gif", ".tif", ".tiff",
    ".pnm", ".pgm", ".pbm", ".ppm", ".pam", ".psd", ".webp",
}

log = logging.getLogger(__name__)

_DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Tesseract (through MuPDF's Leptonica) has process-wide state: two OCR calls on
#: *different* documents still collide ("Attempt to use Leptonica from 2 threads
#: at once"), so the per-document lock is not enough. One Tesseract call at a time.
TESSERACT_LOCK = threading.Lock()


class DocumentError(Exception):
    """Raised for user-facing problems (bad file, unknown id, bad page)."""


class OCRUnavailableError(DocumentError):
    """Raised when an OCR engine can't run here (not installed / not configured)."""


#: The engine whose results live in ``ocr/<page>.json`` (the original layout).
DEFAULT_OCR_ENGINE = "tesseract"
_ENGINE_ID_RE = re.compile(r"^[a-z0-9_]+$")

#: Bump when the fields in ``pages.json`` change; older files are then recomputed.
_PAGE_STATS_VERSION = 1


@dataclass
class DocumentMeta:
    id: str
    filename: str
    page_count: int
    filetype: str
    is_pdf: bool
    size_bytes: int
    created_at: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int
    line: int

    @property
    def rect(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]


@dataclass
class TextLayer:
    """Text of one page from one engine, plus word boxes in PDF points."""

    source: str  # "native" | "ocr" (Tesseract) | "ocr:<engine>"
    text: str
    words: list[Word]
    elapsed_ms: float | None = None
    #: engine details worth showing: engine id/label, model, language, warnings
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "text": self.text,
            "elapsed_ms": self.elapsed_ms,
            "meta": self.meta,
            "words": [[*w.rect, w.text, w.block, w.line] for w in self.words],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TextLayer:
        words = [Word(*row[:4], text=row[4], block=row[5], line=row[6]) for row in data["words"]]
        return cls(source=data["source"], text=data["text"], words=words, elapsed_ms=data.get("elapsed_ms"),
                   meta=data.get("meta") or {})


@dataclass
class RenderedImage:
    """A rasterised page plus the sizes needed to map pixel boxes back to PDF points."""

    data: bytes
    mime: str  # "image/png" | "image/jpeg" | "raw" (packed RGB samples)
    width_px: int
    height_px: int
    page_width: float  # PDF points
    page_height: float


def _words_from_page(page: pymupdf.Page, textpage: pymupdf.TextPage | None = None) -> list[Word]:
    raw = page.get_text("words", textpage=textpage, sort=True)
    return [Word(x0, y0, x1, y1, text, block, line) for (x0, y0, x1, y1, text, block, line, _wno) in raw]


class OpenDocument:
    """A stored document with a lazily opened PyMuPDF handle."""

    def __init__(self, folder: Path, meta: DocumentMeta) -> None:
        self.folder = folder
        self.meta = meta
        self._lock = threading.RLock()
        self._doc: pymupdf.Document | None = None
        self._page_stats: list[dict[str, Any]] | None = None
        # Guards computing page_stats() only, so a long pass doesn't hold the PyMuPDF lock (and block renders).
        self._stats_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    @property
    def source_path(self) -> Path:
        return next(self.folder.glob("source.*"))

    def _handle(self) -> pymupdf.Document:
        if self._doc is None or self._doc.is_closed:
            self._doc = pymupdf.open(self.source_path)
        return self._doc

    def close(self) -> None:
        with self._lock:
            if self._doc is not None and not self._doc.is_closed:
                self._doc.close()
            self._doc = None

    def _page(self, number: int) -> pymupdf.Page:
        if not 0 <= number < self.meta.page_count:
            raise DocumentError(f"page {number} out of range (document has {self.meta.page_count} pages)")
        return self._handle()[number]

    # -- page info / rendering ----------------------------------------------
    @property
    def _page_stats_path(self) -> Path:
        return self.folder / "pages.json"

    def page_stats(self) -> list[dict[str, Any]]:
        """Facts about every page that never change for a stored file (size, text-layer chars, images).

        Reading them takes a full text extraction per page (seconds for a few thousand pages), so they are
        computed once and kept in ``pages.json``. The file is derived data: a missing, stale or damaged one is
        simply rebuilt, so folders from older versions keep working.
        """
        if self._page_stats is not None:
            return self._page_stats
        with self._stats_lock:  # a second caller waits for the first pass instead of repeating it
            if self._page_stats is not None:
                return self._page_stats
            stats = self._load_page_stats()
            if stats is None:
                stats = []
                for n in range(self.meta.page_count):
                    # Lock per page, not for the whole pass: page renders interleave with a
                    # background pass over a large document instead of waiting for all of it.
                    with self._lock:
                        stats.append(self._compute_page_stats(self._handle()[n]))
                    time.sleep(0)  # let a waiting render take the lock; re-acquiring at once would starve it
                tmp = self._page_stats_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps({"version": _PAGE_STATS_VERSION, "pages": stats}, separators=(",", ":")))
                tmp.replace(self._page_stats_path)
            self._page_stats = stats
            return stats

    def _load_page_stats(self) -> list[dict[str, Any]] | None:
        try:
            data = json.loads(self._page_stats_path.read_text())
        except (OSError, ValueError):
            return None
        pages = data.get("pages") if isinstance(data, dict) else None
        if data.get("version") != _PAGE_STATS_VERSION or not isinstance(pages, list) \
                or len(pages) != self.meta.page_count:
            return None
        return pages

    @staticmethod
    def _compute_page_stats(page: pymupdf.Page) -> dict[str, Any]:
        native_chars = len(page.get_text().strip())
        image_count = len(page.get_images(full=False))
        return {
            "width": round(page.rect.width, 2),
            "height": round(page.rect.height, 2),
            "rotation": page.rotation,
            "native_chars": native_chars,
            "image_count": image_count,
            # A page with images but (almost) no text layer is almost
            # certainly a scan and needs OCR to be searchable. Raster
            # uploads (PNG/JPEG/...) never have a text layer.
            "looks_scanned": (native_chars < 20 and image_count > 0) or native_chars == 0,
        }

    def page_info(self, number: int) -> dict[str, Any]:
        if not 0 <= number < self.meta.page_count:
            raise DocumentError(f"page {number} out of range (document has {self.meta.page_count} pages)")
        return self.page_infos()[number]

    def page_infos(self) -> list[dict[str, Any]]:
        """:meth:`page_stats` plus which caches each page has, for every page.

        Scans the ``ocr/`` and ``llm/`` folders once instead of probing them page by page.
        """
        stats = self.page_stats()
        ocr: dict[int, list[str]] = {}
        ocr_root = self.folder / "ocr"
        if ocr_root.is_dir():
            for f in ocr_root.glob("*.json"):  # Tesseract, the original layout
                if f.stem.isdigit():
                    ocr.setdefault(int(f.stem), []).append(DEFAULT_OCR_ENGINE)
            for d in sorted(e for e in ocr_root.iterdir() if e.is_dir()):
                for f in d.glob("*.json"):
                    if f.stem.isdigit():
                        ocr.setdefault(int(f.stem), []).append(d.name)
        llm: dict[int, int] = {}
        llm_root = self.folder / "llm"
        if llm_root.is_dir():
            for d in llm_root.iterdir():
                if d.is_dir() and d.name.isdigit():
                    llm[int(d.name)] = sum(1 for _ in d.glob("*.json"))
        return [
            {"number": n, **s, "has_ocr_cache": DEFAULT_OCR_ENGINE in ocr.get(n, ()),
             "ocr_engines": ocr.get(n, []), "llm_results": llm.get(n, 0)}
            for n, s in enumerate(stats)
        ]

    def render(self, number: int, dpi: int, fmt: str = "png", max_edge_px: int | None = None,
               jpg_quality: int = 90) -> bytes:
        """Rasterise a page. ``fmt`` is ``png``, ``jpeg`` or ``ppm`` (uncompressed, for further processing)."""
        return self.render_image(number, dpi, fmt, max_edge_px, jpg_quality).data

    def render_image(self, number: int, dpi: int, fmt: str = "png", max_edge_px: int | None = None,
                     jpg_quality: int = 90) -> RenderedImage:
        """Like :meth:`render`, plus pixel and page sizes. ``fmt`` may also be ``raw`` (RGB samples)."""
        with self._lock:
            page = self._page(number)
            if max_edge_px:
                # Pick the largest DPI <= requested that keeps the long edge in bounds.
                long_edge_pt = max(page.rect.width, page.rect.height)
                dpi = max(36, min(dpi, int(max_edge_px * 72 / long_edge_pt)))
            pix = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB)
            if fmt == "jpeg":
                data, mime = pix.tobytes("jpeg", jpg_quality=jpg_quality), "image/jpeg"
            elif fmt == "ppm":
                data, mime = pix.tobytes("ppm"), "image/x-portable-pixmap"
            elif fmt == "raw":
                data, mime = bytes(pix.samples), "raw"
            else:
                data, mime = pix.tobytes("png"), "image/png"
            return RenderedImage(data, mime, pix.width, pix.height, page.rect.width, page.rect.height)

    # -- text layers ----------------------------------------------------------
    def native_text(self, number: int) -> TextLayer:
        """Text that is already embedded in the file (no OCR)."""
        with self._lock:
            page = self._page(number)
            t0 = time.perf_counter()
            text = page.get_text("text", sort=True)
            words = _words_from_page(page)
            return TextLayer("native", text, words, elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))

    def _ocr_cache_path(self, number: int, engine: str = DEFAULT_OCR_ENGINE) -> Path:
        if engine == DEFAULT_OCR_ENGINE:
            return self.folder / "ocr" / f"{number}.json"
        if not _ENGINE_ID_RE.match(engine):
            raise DocumentError(f"bad OCR engine id '{engine}'")
        return self.folder / "ocr" / engine / f"{number}.json"

    def ocr_engines_cached(self, number: int) -> list[str]:
        """Ids of the OCR engines that have a cached result for this page."""
        engines = [DEFAULT_OCR_ENGINE] if self._ocr_cache_path(number).exists() else []
        root = self.folder / "ocr"
        if root.is_dir():
            engines += sorted(d.name for d in root.iterdir() if d.is_dir() and (d / f"{number}.json").exists())
        return engines

    def save_ocr_layer(self, number: int, engine: str, layer: TextLayer) -> None:
        cache = self._ocr_cache_path(number, engine)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(layer.to_json(), ensure_ascii=False))

    def ocr_text(self, number: int, *, dpi: int, language: str, force: bool = False) -> TextLayer:
        """Tesseract OCR via PyMuPDF. Results are cached on disk because OCR is slow."""
        cache = self._ocr_cache_path(number)
        if cache.exists() and not force:
            return TextLayer.from_json(json.loads(cache.read_text()))
        with self._lock:
            page = self._page(number)
            t0 = time.perf_counter()
            try:
                # full=True OCRs the whole page image, ignoring any existing text
                # layer: that is what we want when judging OCR quality.
                with TESSERACT_LOCK:
                    tp = page.get_textpage_ocr(full=True, dpi=dpi, language=language)
            except RuntimeError as exc:  # raised when tesseract / tessdata is missing
                raise OCRUnavailableError(
                    f"Tesseract OCR is not available ({exc}). Install tesseract-ocr and the "
                    f"'{language}' language pack, or set TESSDATA_PREFIX."
                ) from exc
            text = page.get_text("text", textpage=tp, sort=True)
            words = _words_from_page(page, textpage=tp)
            layer = TextLayer("ocr", text, words, elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                              meta={"engine": DEFAULT_OCR_ENGINE, "language": language, "dpi": dpi})
        self.save_ocr_layer(number, DEFAULT_OCR_ENGINE, layer)
        return layer

    def cached_ocr_text(self, number: int, engine: str = DEFAULT_OCR_ENGINE) -> TextLayer | None:
        cache = self._ocr_cache_path(number, engine)
        return TextLayer.from_json(json.loads(cache.read_text())) if cache.exists() else None

    # -- LLM result cache -----------------------------------------------------
    def _llm_dir(self, number: int) -> Path:
        return self.folder / "llm" / str(number)

    @staticmethod
    def llm_cache_key(provider: str, model: str, prompt: str, preprocess: str = "") -> str:
        """Stable id of one LLM configuration, identical on every page of a document.

        ``preprocess`` is the canonical image-preprocessing spec. It is only mixed
        into the digest when non-empty, so runs on the plain page image keep the
        keys they had before preprocessing existed (no orphaned caches).
        """
        material = f"{provider}\0{model}\0{prompt}" + (f"\0{preprocess}" if preprocess else "")
        digest = hashlib.sha256(material.encode()).hexdigest()[:16]
        safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", model)[:60]
        return f"{provider}__{safe_model}__{digest}"

    def save_llm_result(self, number: int, key: str, result: dict[str, Any]) -> None:
        folder = self._llm_dir(number)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{key}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))

    def load_llm_result(self, number: int, key: str) -> dict[str, Any] | None:
        path = self._llm_dir(number) / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def list_llm_results(self, number: int) -> list[dict[str, Any]]:
        results = []
        for path in self._llm_dir(number).glob("*.json"):
            data = json.loads(path.read_text())
            data["key"] = path.stem
            results.append(data)
        return sorted(results, key=lambda r: r.get("created_at", 0), reverse=True)

    def iter_pages(self) -> Iterator[int]:
        return iter(range(self.meta.page_count))


class DocumentStore:
    """Persists uploads under ``root`` and hands out :class:`OpenDocument` objects."""

    def __init__(self, root: Path, max_open: int = 16) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._open: OrderedDict[str, OpenDocument] = OrderedDict()
        self._max_open = max_open
        self._lock = threading.Lock()
        self._warm_queue: queue.Queue[str] = queue.Queue()
        self._warm_thread: threading.Thread | None = None

    def add(self, filename: str, data: bytes) -> DocumentMeta:
        ext = Path(filename).suffix.lower() or ".pdf"
        if ext not in SUPPORTED_EXTENSIONS:
            raise DocumentError(f"unsupported file type '{ext}'")
        try:
            doc = pymupdf.open(stream=data, filetype=ext.lstrip("."))
        except Exception as exc:  # PyMuPDF raises several exception types here
            raise DocumentError(f"could not open '{filename}': {exc}") from exc
        try:
            if doc.needs_pass:
                raise DocumentError("document is password protected")
            if doc.page_count == 0:
                raise DocumentError("document has no pages")
            meta = DocumentMeta(
                id=uuid.uuid4().hex,
                filename=Path(filename).name,
                page_count=doc.page_count,
                filetype=ext.lstrip("."),
                is_pdf=doc.is_pdf,
                size_bytes=len(data),
                created_at=time.time(),
                metadata={k: v for k, v in (doc.metadata or {}).items() if v},
            )
        finally:
            doc.close()
        folder = self.root / meta.id
        folder.mkdir(parents=True)
        (folder / f"source{ext}").write_bytes(data)
        (folder / "meta.json").write_text(json.dumps(asdict(meta), indent=1))
        return meta

    def get(self, doc_id: str) -> OpenDocument:
        if not _DOC_ID_RE.match(doc_id):
            raise DocumentError("unknown document")
        with self._lock:
            if doc_id in self._open:
                self._open.move_to_end(doc_id)
                return self._open[doc_id]
            folder = self.root / doc_id
            meta_path = folder / "meta.json"
            if not meta_path.exists():
                raise DocumentError("unknown document")
            doc = OpenDocument(folder, DocumentMeta(**json.loads(meta_path.read_text())))
            self._open[doc_id] = doc
            while len(self._open) > self._max_open:
                _, evicted = self._open.popitem(last=False)
                evicted.close()
            return doc

    def warm_page_stats(self, doc_id: str) -> None:
        """Queue building ``pages.json`` ahead of the first Overview visit; returns at once.

        One daemon worker does the queued documents in order, outside any request, so a batch upload doesn't
        start a thread per file and a slow pass never holds up the next request on the uploader's connection.
        Best effort: if the document is deleted meanwhile, or anything else fails, the next visit computes it.
        """
        with self._lock:
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(target=self._warm_worker, name="pyocr-page-stats", daemon=True)
                self._warm_thread.start()
        self._warm_queue.put(doc_id)

    def wait_for_warmups(self) -> None:
        """Block until every queued :meth:`warm_page_stats` has finished (for tests and the CLI)."""
        self._warm_queue.join()

    def _warm_worker(self) -> None:
        while True:
            doc_id = self._warm_queue.get()
            try:
                self.get(doc_id).page_stats()
            except Exception:  # noqa: BLE001 - a warm-up must never surface as an error
                log.debug("could not precompute page stats for %s", doc_id, exc_info=True)
            finally:
                self._warm_queue.task_done()

    def list(self) -> list[DocumentMeta]:
        metas = []
        for meta_path in self.root.glob("*/meta.json"):
            try:
                metas.append(DocumentMeta(**json.loads(meta_path.read_text())))
            except (ValueError, TypeError):
                continue
        return sorted(metas, key=lambda m: m.created_at, reverse=True)

    def delete(self, doc_id: str) -> None:
        doc = self.get(doc_id)
        with self._lock:
            self._open.pop(doc_id, None)
        doc.close()
        shutil.rmtree(doc.folder)
