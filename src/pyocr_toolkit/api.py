"""JSON API (under ``/api``) and the application factory.

Run with ``pyocr serve`` (or ``uvicorn pyocr_toolkit.api:app``), then open
http://127.0.0.1:8000. Interactive API docs live at ``/docs``.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, compare, search
from .brands import brands_json
from .config import Settings, get_settings
from .documents import DEFAULT_OCR_ENGINE, DocumentError, DocumentStore, OCRUnavailableError, OpenDocument
from .jobs import JobManager
from .llm import PROMPT_MODES, ProviderError, build_providers
from .ocr import build_ocr_engines
from .service import LLMSpec, Services, ocr_source_id, parse_pages

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------- bodies
class LLMRunBody(BaseModel):
    provider: str
    model: str | None = None
    prompt_mode: str = "verbatim"
    custom_prompt: str | None = None
    force: bool = False

    def spec(self) -> LLMSpec:
        return LLMSpec(self.provider, self.model, self.prompt_mode, self.custom_prompt)


class TextRef(BaseModel):
    source: Literal["native", "ocr", "llm"]
    #: for ``llm``: cache key of a stored result (``None`` -> most recent one)
    key: str | None = None
    #: for ``ocr``: engine id (``None`` -> Tesseract)
    engine: str | None = None

    @property
    def source_id(self) -> str:
        if self.source == "ocr":
            return ocr_source_id(self.engine or DEFAULT_OCR_ENGINE)
        return f"llm:{self.key}" if self.source == "llm" and self.key else self.source


class CompareBody(BaseModel):
    reference: TextRef = Field(default_factory=lambda: TextRef(source="llm"))
    hypothesis: TextRef = Field(default_factory=lambda: TextRef(source="ocr"))
    ignore_case: bool = False
    ignore_punctuation: bool = False
    diff: bool = True


class JobBody(BaseModel):
    kind: Literal["ocr", "llm"]
    pages: str | None = Field(None, description="1-based page ranges like '1-3,7'; empty = all pages")
    force: bool = False
    llm: LLMRunBody | None = None
    engine: str = Field(DEFAULT_OCR_ENGINE, description="OCR engine id for kind=ocr (see /api/config)")


class ProviderTestBody(BaseModel):
    model: str | None = None


def _safe_stem(filename: str) -> str:
    # ASCII-only filename: HTTP headers are latin-1 and quotes would break them.
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem) or "document"


def build_api(settings: Settings, store: DocumentStore, services: Services, jobs: JobManager) -> APIRouter:
    api = APIRouter(prefix="/api")
    providers = services.providers
    engines = services.ocr_engines

    def get_doc(doc_id: str) -> OpenDocument:
        return store.get(doc_id)

    def doc_summary(doc: OpenDocument) -> dict:
        """Meta + extraction coverage, used by the library and overview pages."""
        n = doc.meta.page_count
        ocr_pages = llm_pages = 0
        for p in range(n):
            ocr_pages += bool(doc.ocr_engines_cached(p))
            llm_pages += bool(doc.list_llm_results(p))
        return {**doc.meta.__dict__, "ocr_pages": ocr_pages, "llm_pages": llm_pages}

    # ------------------------------------------------------------------ config
    @api.get("/config", tags=["config"])
    def config():
        return {
            "version": __version__,
            "providers": [p.describe() for p in providers.values()],
            "prompt_modes": [{"id": k, "label": v["label"], "prompt": v["system"]} for k, v in PROMPT_MODES.items()],
            "tesseract_available": shutil.which("tesseract") is not None,
            "ocr_engines": [e.describe() for e in engines.values()],
            "brands": brands_json(),
            "tesseract_language": settings.tesseract_language,
            "max_upload_mb": settings.max_upload_mb,
            "preview_dpi": settings.preview_dpi,
            "llm_dpi": settings.llm_dpi,
            "ocr_dpi": settings.ocr_dpi,
        }

    @api.post("/providers/{provider_id}/test", tags=["config"])
    async def test_provider(provider_id: str, body: ProviderTestBody):
        try:
            return await services.test_provider(provider_id, body.model)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/ocr-engines/{engine_id}/test", tags=["config"])
    async def test_ocr_engine(engine_id: str):
        """OCR a small generated image with one engine and report what it read."""
        if engine_id not in engines:
            raise HTTPException(404, f"unknown OCR engine '{engine_id}'")
        return await services.test_ocr_engine(engine_id)

    # --------------------------------------------------------------- documents
    @api.get("/documents", tags=["documents"])
    def list_documents(details: bool = False):
        metas = store.list()
        if not details:
            return [m.__dict__ for m in metas]
        return [doc_summary(store.get(m.id)) for m in metas]

    @api.post("/documents", status_code=201, tags=["documents"])
    async def upload(file: UploadFile = File(...)):
        data = await file.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"file larger than {settings.max_upload_mb} MB")
        meta = await run_in_threadpool(store.add, file.filename or "upload.pdf", data)
        return meta.__dict__

    @api.get("/documents/{doc_id}", tags=["documents"])
    def document(doc_id: str):
        return doc_summary(get_doc(doc_id))

    @api.delete("/documents/{doc_id}", status_code=204, tags=["documents"])
    def delete_document(doc_id: str):
        store.delete(doc_id)
        return Response(status_code=204)

    @api.get("/documents/{doc_id}/source", tags=["documents"])
    def source_file(doc_id: str):
        doc = get_doc(doc_id)
        return FileResponse(doc.source_path, filename=doc.meta.filename)

    @api.get("/documents/{doc_id}/pages", tags=["pages"])
    def pages(doc_id: str):
        doc = get_doc(doc_id)
        return [doc.page_info(n) for n in doc.iter_pages()]

    @api.get("/documents/{doc_id}/pages/{page}/image", tags=["pages"])
    def page_image(doc_id: str, page: int, dpi: int = Query(None, ge=36, le=600)):
        png = get_doc(doc_id).render(page, dpi or settings.preview_dpi)
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @api.get("/documents/{doc_id}/sources", tags=["text"])
    def sources(doc_id: str):
        """Text sources available for the document (native, OCR, each LLM config) with page coverage."""
        return services.list_sources(get_doc(doc_id))

    # ------------------------------------------------------------- text layers
    @api.get("/documents/{doc_id}/pages/{page}/text/native", tags=["text"])
    def native_text(doc_id: str, page: int):
        return get_doc(doc_id).native_text(page).to_json()

    @api.get("/documents/{doc_id}/pages/{page}/text/ocr", tags=["text"])
    def ocr_text(doc_id: str, page: int, force: bool = False, cached_only: bool = False,
                 engine: str = Query(DEFAULT_OCR_ENGINE, description="OCR engine id (see /api/config)")):
        """OCR one page (cached per engine). ``cached_only`` never runs the engine."""
        doc = get_doc(doc_id)
        services.ocr_engine(engine)  # 400 for an unknown engine
        if cached_only:
            layer = doc.cached_ocr_text(page, engine)
            return layer.to_json() if layer else None
        return services.run_ocr_page(doc, page, force=force, engine=engine).to_json()

    @api.get("/documents/{doc_id}/text", tags=["text"])
    def document_text(doc_id: str, source: str = "native"):
        """Every page's text from one source. Missing pages have ``text: null``."""
        doc = get_doc(doc_id)
        return {
            "source": source,
            "label": services.source_label(doc, source),
            "pages": [{"page": n, "text": services.page_text(doc, n, source)} for n in doc.iter_pages()],
        }

    # --------------------------------------------------------------------- LLM
    @api.get("/documents/{doc_id}/pages/{page}/llm", tags=["llm"])
    def llm_results(doc_id: str, page: int):
        return get_doc(doc_id).list_llm_results(page)

    @api.post("/documents/{doc_id}/pages/{page}/llm", tags=["llm"])
    async def run_llm(doc_id: str, page: int, body: LLMRunBody):
        doc = get_doc(doc_id)
        if not 0 <= page < doc.meta.page_count:
            raise DocumentError(f"page {page} out of range")
        try:
            services.resolve_spec(body.spec())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return await services.run_llm_page(doc, page, body.spec(), force=body.force)

    @api.delete("/documents/{doc_id}/pages/{page}/llm/{key}", status_code=204, tags=["llm"])
    def delete_llm_result(doc_id: str, page: int, key: str):
        doc = get_doc(doc_id)
        folder = (doc.folder / "llm" / str(page)).resolve()
        path = (folder / f"{key}.json").resolve()
        if path.parent != folder or not path.exists():
            raise HTTPException(404, "no such result")
        path.unlink()
        return Response(status_code=204)

    # -------------------------------------------------------------------- jobs
    @api.post("/documents/{doc_id}/jobs", status_code=202, tags=["jobs"])
    async def start_job(doc_id: str, body: JobBody):
        """Extract many pages in the background (OCR or an LLM). Poll ``/api/jobs/{id}``."""
        doc = get_doc(doc_id)
        try:
            pages_list = parse_pages(body.pages, doc.meta.page_count)
            job = jobs.start(doc.meta.id, body.kind, pages_list,
                             llm=body.llm.spec() if body.llm else None, force=body.force, engine=body.engine)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return job.to_json()

    @api.get("/jobs", tags=["jobs"])
    def list_jobs(doc_id: str | None = None):
        return [j.to_json() for j in jobs.list(doc_id)]

    @api.get("/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: str):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job.to_json()

    @api.post("/jobs/{job_id}/cancel", tags=["jobs"])
    def cancel_job(job_id: str):
        job = jobs.cancel(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job.to_json()

    # ----------------------------------------------------------------- compare
    @api.post("/documents/{doc_id}/pages/{page}/compare", tags=["compare"])
    def compare_page(doc_id: str, page: int, body: CompareBody):
        doc = get_doc(doc_id)
        def text_of(ref: TextRef) -> str:
            # OCR is run on demand (and cached); LLM results must exist already.
            text = services.page_text(doc, page, ref.source_id, run_ocr=True)
            if text is None:
                raise DocumentError(f"no LLM transcription for page {page + 1} yet - run one first")
            return text

        ref_text = text_of(body.reference)
        hyp_text = text_of(body.hypothesis)
        opts = compare.NormalizeOptions(body.ignore_case, body.ignore_punctuation)
        return {
            "reference": {"label": services.source_label(doc, body.reference.source_id), **body.reference.model_dump()},
            "hypothesis": {"label": services.source_label(doc, body.hypothesis.source_id), **body.hypothesis.model_dump()},
            "metrics": compare.score(ref_text, hyp_text, opts),
            "diff": compare.word_diff(ref_text, hyp_text, opts) if body.diff else None,
        }

    @api.get("/documents/{doc_id}/compare", tags=["compare"])
    def compare_document(doc_id: str, a: str = Query(..., description="reference source id"),
                         b: str = Query(..., description="candidate source id"),
                         ignore_case: bool = True, ignore_punctuation: bool = False):
        """Score source ``b`` against source ``a`` on every page where both exist.

        ``overall`` is micro-averaged (total edits / total reference chars), so
        long pages weigh more than short ones - the honest whole-document number.
        """
        doc = get_doc(doc_id)
        opts = compare.NormalizeOptions(ignore_case, ignore_punctuation)
        rows, tot_cd, tot_rc, tot_wd, tot_rw = [], 0, 0, 0, 0
        for n in doc.iter_pages():
            ta, tb = services.page_text(doc, n, a), services.page_text(doc, n, b)
            if ta is None or tb is None:
                rows.append({"page": n, "metrics": None, "missing": [s for s, t in ((a, ta), (b, tb)) if t is None]})
                continue
            m = compare.score(ta, tb, opts)
            tot_cd += m["char_distance"]; tot_rc += m["reference_chars"]
            tot_wd += m["word_distance"]; tot_rw += m["reference_words"]
            rows.append({"page": n, "metrics": m, "missing": []})
        scored = [r for r in rows if r["metrics"]]
        overall = None
        if scored:
            overall = {
                "pages_scored": len(scored),
                "cer": round(tot_cd / tot_rc, 4) if tot_rc else None,
                "wer": round(tot_wd / tot_rw, 4) if tot_rw else None,
                "worst_page": max(scored, key=lambda r: r["metrics"]["cer"])["page"],
                "pages_over_10pct": sum(r["metrics"]["cer"] > 0.10 for r in scored),
            }
        return {"a": {"id": a, "label": services.source_label(doc, a)},
                "b": {"id": b, "label": services.source_label(doc, b)},
                "overall": overall, "pages": rows}

    @api.get("/documents/{doc_id}/report", tags=["compare"])
    def report(doc_id: str, ignore_case: bool = True, ignore_punctuation: bool = False):
        """Per-page overview against the most recent LLM transcription of each page."""
        doc = get_doc(doc_id)
        opts = compare.NormalizeOptions(ignore_case, ignore_punctuation)
        rows = []
        for n in doc.iter_pages():
            native = doc.native_text(n).text
            ocr = doc.cached_ocr_text(n)
            llm = doc.list_llm_results(n)
            row = {"page": n, "native_chars": len(native.strip()),
                   "ocr_chars": len(ocr.text.strip()) if ocr else None, "llm_runs": len(llm),
                   "llm_model": f"{llm[0]['provider']} / {llm[0]['model']}" if llm else None,
                   "native_vs_llm": None, "ocr_vs_llm": None, "native_vs_ocr": None}
            if llm:
                if native.strip():
                    row["native_vs_llm"] = compare.score(llm[0]["text"], native, opts)
                if ocr:
                    row["ocr_vs_llm"] = compare.score(llm[0]["text"], ocr.text, opts)
            if ocr and native.strip():
                row["native_vs_ocr"] = compare.score(native, ocr.text, opts)
            rows.append(row)
        return {"document": doc.meta.__dict__, "pages": rows}

    # ------------------------------------------------------------------ search
    def _search_doc(doc: OpenDocument, q: str, wanted: set[str], limit_per_page: int, boxes: bool = True):
        pages_out, total, ocr_missing = [], 0, 0
        for n in doc.iter_pages():
            hits = []
            if "native" in wanted:
                layer = doc.native_text(n)
                if count := search.count_matches(layer.text, q):
                    hits.append({"source": "native", "label": "Native text", "count": count,
                                 "boxes": search.find_boxes(layer.words, q) if boxes else [],
                                 "snippets": search.find_snippets(layer.text, q, limit=limit_per_page)})
            if "ocr" in wanted:
                cached = doc.ocr_engines_cached(n)
                if not cached:
                    ocr_missing += 1
                for engine in cached:
                    layer = doc.cached_ocr_text(n, engine)
                    if layer and (count := search.count_matches(layer.text, q)):
                        hits.append({"source": "ocr", "engine": engine, "label": services.ocr_label(engine),
                                     "count": count, "boxes": search.find_boxes(layer.words, q) if boxes else [],
                                     "snippets": search.find_snippets(layer.text, q, limit=limit_per_page)})
            if "llm" in wanted:
                for r in doc.list_llm_results(n):
                    if count := search.count_matches(r["text"], q):
                        hits.append({"source": "llm", "key": r["key"], "provider": r["provider"],
                                     "label": f"LLM: {r['provider']} / {r['model']}",
                                     "count": count, "boxes": [],
                                     "snippets": search.find_snippets(r["text"], q, limit=limit_per_page)})
            if hits:
                total += sum(h["count"] for h in hits)
                pages_out.append({"page": n, "hits": hits})
        return pages_out, total, ocr_missing

    @api.get("/documents/{doc_id}/search", tags=["search"])
    def search_document(doc_id: str, q: str = Query(..., min_length=1), sources: str = "native,ocr,llm",
                        limit_per_page: int = Query(10, ge=1, le=100)):
        """Search every page. OCR is only searched where an engine has already run (it's slow)."""
        wanted = {s.strip() for s in sources.split(",") if s.strip()}
        pages_out, total, missing = _search_doc(get_doc(doc_id), q, wanted, limit_per_page)
        return {"query": q, "total": total, "pages": pages_out, "pages_without_ocr": missing}

    @api.get("/search", tags=["search"])
    def search_library(q: str = Query(..., min_length=1), sources: str = "native,ocr,llm",
                       max_documents: int = Query(50, ge=1, le=500)):
        """Search all documents in the library (snippets only, no boxes)."""
        wanted = {s.strip() for s in sources.split(",") if s.strip()}
        results, total = [], 0
        for meta in store.list()[:max_documents]:
            pages_out, doc_total, _ = _search_doc(store.get(meta.id), q, wanted, 3, boxes=False)
            if pages_out:
                total += doc_total
                results.append({"document": meta.__dict__, "total": doc_total, "pages": pages_out[:25]})
        return {"query": q, "total": total, "documents": results}

    # ------------------------------------------------------------------ export
    @api.get("/documents/{doc_id}/export", tags=["text"])
    def export(doc_id: str, source: str = "llm", fmt: Literal["txt", "md", "json", "jsonl"] = "txt",
               key: str | None = None):
        """Download the whole document's text from one source.

        ``source`` is ``native``, ``ocr`` (Tesseract), ``ocr:<engine>``, ``llm`` (latest per page) or ``llm:<key>``.
        ``jsonl`` writes one JSON object per page - handy for RAG / ingestion pipelines.
        Pages without a result for that source are empty (nothing is run implicitly).
        """
        doc = get_doc(doc_id)
        if key and source == "llm":
            source = f"llm:{key}"
        texts = [services.page_text(doc, n, source) for n in doc.iter_pages()]
        stem = _safe_stem(doc.meta.filename)
        tag = source.replace(":", "-") if source.startswith("ocr:") else source.split(":")[0]
        disp = lambda ext: {"Content-Disposition": f'attachment; filename="{stem}.{tag}.{ext}"'}  # noqa: E731
        label = services.source_label(doc, source)
        if fmt == "json":
            return JSONResponse({"document": doc.meta.__dict__, "source": source, "source_label": label,
                                 "pages": [{"page": i + 1, "text": t, "extracted": t is not None}
                                           for i, t in enumerate(texts)]}, headers=disp("json"))
        if fmt == "jsonl":
            lines = (json.dumps({"document_id": doc.meta.id, "filename": doc.meta.filename, "page": i + 1,
                                 "source": source, "text": t or ""}, ensure_ascii=False)
                     for i, t in enumerate(texts))
            return PlainTextResponse("\n".join(lines) + "\n", media_type="application/x-ndjson", headers=disp("jsonl"))
        if fmt == "md":
            body = f"# {doc.meta.filename}\n\n_Source: {label}_\n" + "".join(
                f"\n\n## Page {i + 1}\n\n{(t or '_(not extracted)_').strip()}" for i, t in enumerate(texts))
            return PlainTextResponse(body + "\n", media_type="text/markdown", headers=disp("md"))
        body = "".join(f"\n\n===== Page {i + 1} =====\n\n{(t or '').strip()}" for i, t in enumerate(texts)).lstrip()
        return PlainTextResponse(body, headers=disp("txt"))

    return api


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = DocumentStore(settings.documents_dir)
    providers = build_providers(settings)
    services = Services(settings, providers, build_ocr_engines(settings))
    jobs = JobManager(store, services)

    app = FastAPI(title="pyocr-toolkit", version=__version__,
                  description="Inspect and compare PDF/image text extraction: native text, OCR engines and LLMs.")
    app.state.settings = settings
    app.state.store = store
    app.state.providers = providers
    app.state.services = services
    app.state.jobs = jobs

    @app.exception_handler(DocumentError)
    async def _doc_error(request: Request, exc: DocumentError):
        status = 503 if isinstance(exc, OCRUnavailableError) else 404 if "unknown document" in str(exc) else 400
        if not request.url.path.startswith("/api"):
            from .web import render_error
            return render_error(request, status, str(exc))
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.exception_handler(ProviderError)
    async def _provider_error(_req, exc: ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    app.include_router(build_api(settings, store, services, jobs))
    from .web import build_web

    app.include_router(build_web(settings, store, services, jobs))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def __getattr__(name: str):
    # ``uvicorn pyocr_toolkit.api:app`` support without building the app at import time.
    if name == "app":
        return create_app()
    raise AttributeError(name)
