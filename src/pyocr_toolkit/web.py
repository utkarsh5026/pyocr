"""Server-rendered HTML pages.

Each page is a normal URL you can bookmark, share or open in a new tab. The
templates render the layout and anything cheap to compute; interactive parts
(running OCR/LLMs, diffs, search) are small per-page scripts talking to the
JSON API in :mod:`pyocr_toolkit.api`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import Settings
from .documents import DocumentStore
from .jobs import JobManager
from .service import Services

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _filesize(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _datetime(ts: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


TEMPLATES.env.filters["filesize"] = _filesize
TEMPLATES.env.filters["datetime"] = _datetime
TEMPLATES.env.globals["version"] = __version__


def render_error(request: Request, status: int, message: str) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "error.html", {"status": status, "message": message, "nav": ""},
                                      status_code=status)


def build_web(settings: Settings, store: DocumentStore, services: Services, jobs: JobManager) -> APIRouter:
    web = APIRouter(include_in_schema=False)

    def page(request: Request, template: str, nav: str, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, template, {"nav": nav, **ctx})

    # Pages with a usable embedded text layer never change for a stored file,
    # so remember them per document instead of re-reading every page.
    native_cache: dict[str, frozenset[int]] = {}

    def summary(doc_id: str) -> dict:
        doc = store.get(doc_id)
        n = doc.meta.page_count
        if doc_id not in native_cache:
            native_cache[doc_id] = frozenset(p for p in range(n) if not doc.page_info(p)["looks_scanned"])
        native = native_cache[doc_id]
        ocr = {p for p in range(n) if doc.cached_ocr_text(p) is not None}
        llm = {p for p in range(n) if doc.list_llm_results(p)}
        missing = n - len(native | ocr | llm)  # pages with no text from any source
        status = "complete" if missing == 0 else "scanned" if not native else "partial"
        return {**doc.meta.__dict__, "native_pages": len(native), "ocr_pages": len(ocr),
                "llm_pages": len(llm), "missing_pages": missing, "status": status}

    @web.get("/", response_class=HTMLResponse)
    def home(request: Request):
        recent = [summary(m.id) for m in store.list()[:6]]
        configured = [p for p in services.providers.values() if p.configured]
        return page(request, "home.html", "home", recent=recent, configured=configured,
                    providers=list(services.providers.values()),
                    tesseract=shutil.which("tesseract") is not None, total_docs=len(store.list()))

    @web.get("/documents", response_class=HTMLResponse)
    def library(request: Request):
        return page(request, "library.html", "library", docs=[summary(m.id) for m in store.list()])

    @web.get("/documents/{doc_id}", response_class=HTMLResponse)
    def overview(request: Request, doc_id: str):
        doc = store.get(doc_id)
        return page(request, "document.html", "library", doc=summary(doc_id),
                    sources=services.list_sources(doc), doc_tab="overview")

    @web.get("/documents/{doc_id}/pages/{page_no}", response_class=HTMLResponse)
    def workspace(request: Request, doc_id: str, page_no: int):
        doc = store.get(doc_id)
        page_no = max(1, min(page_no, doc.meta.page_count))
        return page(request, "workspace.html", "library", doc=summary(doc_id), page_no=page_no,
                    doc_tab="pages")

    @web.get("/documents/{doc_id}/pages")
    def workspace_first(doc_id: str):
        store.get(doc_id)
        return RedirectResponse(f"/documents/{doc_id}/pages/1")

    @web.get("/documents/{doc_id}/text", response_class=HTMLResponse)
    def reader(request: Request, doc_id: str):
        doc = store.get(doc_id)
        return page(request, "reader.html", "library", doc=summary(doc_id),
                    sources=services.list_sources(doc), doc_tab="text")

    @web.get("/documents/{doc_id}/compare", response_class=HTMLResponse)
    def compare_view(request: Request, doc_id: str):
        doc = store.get(doc_id)
        return page(request, "compare.html", "library", doc=summary(doc_id),
                    sources=services.list_sources(doc), doc_tab="compare")

    @web.get("/search", response_class=HTMLResponse)
    def search_view(request: Request, q: str = ""):
        return page(request, "search.html", "search", q=q)

    @web.get("/jobs", response_class=HTMLResponse)
    def jobs_view(request: Request):
        names = {m.id: m.filename for m in store.list()}
        return page(request, "jobs.html", "jobs", jobs=[j.to_json() for j in jobs.list()], names=names)

    @web.get("/providers", response_class=HTMLResponse)
    def providers_view(request: Request):
        tesseract = shutil.which("tesseract")
        langs: list[str] = []
        version = ""
        if tesseract:
            try:
                out = subprocess.run([tesseract, "--list-langs"], capture_output=True, text=True, timeout=10)
                langs = [ln.strip() for ln in out.stdout.splitlines()[1:] if ln.strip()]
                ver = subprocess.run([tesseract, "--version"], capture_output=True, text=True, timeout=10)
                version = (ver.stdout or ver.stderr).splitlines()[0] if (ver.stdout or ver.stderr) else ""
            except (OSError, subprocess.SubprocessError):
                pass
        return page(request, "providers.html", "providers", providers=list(services.providers.values()),
                    tesseract=tesseract, tesseract_langs=langs, tesseract_version=version, settings=settings)

    @web.get("/help", response_class=HTMLResponse)
    def help_view(request: Request):
        return page(request, "help.html", "help")

    return web
