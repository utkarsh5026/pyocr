"""JSON API (under ``/api``) and the application factory.

Run with ``pyocr serve`` (or ``uvicorn pyocr_toolkit.api:app``), then open
http://127.0.0.1:8000. Interactive API docs live at ``/docs``.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, compare, export, search
from .brands import brands_json
from .config import Settings, get_settings
from .documents import DEFAULT_OCR_ENGINE, DocumentError, DocumentStore, OCRUnavailableError, OpenDocument
from .jobs import JobManager
from .llm import MAX_PAGES_PER_REQUEST, PROMPT_MODES, ProviderError, build_providers, catalog
from .ocr import build_ocr_engines
from .preprocess import Preprocess, presets_json, process_bytes
from .service import LLMSpec, Services, ocr_source_id, parse_pages
from .workspaces import WorkspaceStore, ingest, suggest_names

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------- bodies
class LLMRunBody(BaseModel):
    provider: str
    model: str | None = None
    prompt_mode: str = "verbatim"
    custom_prompt: str | None = None
    preprocess: str | None = Field(None, description="image preprocessing, e.g. 'gray;contrast=1.5' or a preset id")
    pages_per_request: int = Field(1, ge=1, le=MAX_PAGES_PER_REQUEST,
                                   description="pages sent together in one LLM request (1 = one page per request)")
    force: bool = False

    def spec(self) -> LLMSpec:
        return LLMSpec(self.provider, self.model, self.prompt_mode, self.custom_prompt,
                       preprocess=self.preprocess or "", pages_per_request=self.pages_per_request)


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


class WorkspaceBody(BaseModel):
    name: str | None = Field(None, description="empty = a suggested name such as 'festive-albattani'")
    doc_ids: list[str] = Field(default_factory=list)


class WorkspaceRenameBody(BaseModel):
    name: str


class WorkspaceDocsBody(BaseModel):
    doc_ids: list[str]


class SuggestNamesBody(BaseModel):
    filenames: list[str] = Field(default_factory=list)
    count: int = Field(4, ge=1, le=12)


class ProviderTestBody(BaseModel):
    model: str | None = None


class CredentialsBody(BaseModel):
    #: env var name -> new value; empty / missing = keep the current value
    values: dict[str, str | None] = Field(default_factory=dict)
    #: env var names to forget, falling back to the environment / built-in default
    clear: list[str] = Field(default_factory=list)


def _require_same_origin(request: Request) -> None:
    """Refuse cross-site writes. There's no auth (localhost only), so without this any web
    page open in the same browser could POST new endpoints/keys to the local server."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        from urllib.parse import urlsplit

        if urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-origin request refused; open pyocr in this browser tab instead")
    if not (request.headers.get("content-type") or "").startswith("application/json"):
        raise HTTPException(415, "send JSON (Content-Type: application/json)")


def _safe_stem(filename: str) -> str:
    # ASCII-only filename: HTTP headers are latin-1 and quotes would break them.
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem) or "document"


def build_api(settings: Settings, store: DocumentStore, services: Services, jobs: JobManager,
              workspaces: WorkspaceStore) -> APIRouter:
    api = APIRouter(prefix="/api")
    providers = services.providers
    engines = services.ocr_engines

    def get_doc(doc_id: str) -> OpenDocument:
        return store.get(doc_id)

    def parse_pre(spec: str | None) -> Preprocess:
        try:
            return Preprocess.parse(spec)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

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
            "max_pages_per_request": MAX_PAGES_PER_REQUEST,
            "tesseract_available": shutil.which("tesseract") is not None,
            "ocr_engines": [e.describe() for e in engines.values()],
            "brands": brands_json(),
            "tesseract_language": settings.tesseract_language,
            "max_upload_mb": settings.max_upload_mb,
            "preview_dpi": settings.preview_dpi,
            "llm_dpi": settings.llm_dpi,
            "llm_max_edge_px": settings.llm_max_edge_px,
            "ocr_dpi": settings.ocr_dpi,
            "preprocess_presets": presets_json(),
        }

    @api.get("/models", tags=["models"])
    def search_models(q: str = "", provider: str | None = None, rating: str | None = None,
                      include_deprecated: bool = True):
        """Search the built-in catalog of vision models, rated for page transcription."""
        return {
            "updated": catalog.CATALOG_UPDATED,
            "usd_inr_rate": settings.usd_inr_rate,  # prices are USD; the page also shows them in ₹
            "ratings": {k: v[0] for k, v in catalog.RATINGS.items()},
            "sources": [{"label": a, "url": b} for a, b in catalog.SOURCES],
            "models": [m.to_json() for m in catalog.search(q, provider, rating, include_deprecated)],
        }

    @api.get("/models/lookup", tags=["models"])
    def lookup_model(name: str, provider: str | None = None):
        """How well a (possibly custom) model name suits transcription."""
        return catalog.lookup(name, provider)

    @api.get("/providers/{provider_id}/models", tags=["models"])
    async def provider_models(provider_id: str, live: bool = True):
        """Catalog models for a provider plus, where the API allows, the models your key can use."""
        try:
            return await services.provider_models(provider_id, live)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/providers/{provider_id}/credentials", tags=["config"])
    def get_credentials(provider_id: str):
        p = providers.get(provider_id)
        if p is None:
            raise HTTPException(404, f"unknown provider '{provider_id}'")
        return {"provider": p.describe(), "settings": services.credentials.status(p)}

    @api.post("/providers/{provider_id}/credentials", tags=["config"])
    def save_credentials(provider_id: str, body: CredentialsBody, request: Request):
        """Save provider settings from the UI (stored in DATA_DIR/credentials.json, never echoed back)."""
        _require_same_origin(request)
        try:
            services.credentials.update(provider_id, body.values, body.clear)
        except ValueError as exc:
            status = 404 if "unknown provider" in str(exc) else 400
            raise HTTPException(status, str(exc)) from exc
        return get_credentials(provider_id)

    @api.get("/ocr-engines/{engine_id}/credentials", tags=["config"])
    def get_engine_credentials(engine_id: str):
        e = engines.get(engine_id)
        if e is None:
            raise HTTPException(404, f"unknown OCR engine '{engine_id}'")
        d = e.describe()
        # same shape as the provider endpoint, so the Providers page handles both
        return {"provider": {**d, "configured": d["available"]}, "settings": services.credentials.status(e)}

    @api.post("/ocr-engines/{engine_id}/credentials", tags=["config"])
    def save_engine_credentials(engine_id: str, body: CredentialsBody, request: Request):
        """Save OCR engine settings from the UI (stored in DATA_DIR/credentials.json, never echoed back)."""
        _require_same_origin(request)
        if engine_id not in engines:
            raise HTTPException(404, f"unknown OCR engine '{engine_id}'")
        try:
            services.credentials.update(engine_id, body.values, body.clear)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return get_engine_credentials(engine_id)

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

    @api.post("/uploads", status_code=201, tags=["documents"])
    async def upload_batch(files: list[UploadFile] = File(...), workspace_id: str | None = Form(None)):
        """Upload several files at once; ``.zip`` archives are unpacked. Optionally add them to a workspace.

        Files that can't be opened are reported in ``skipped`` instead of failing the whole batch.
        """
        if workspace_id:
            workspaces.get(workspace_id)  # fail before storing anything
        max_bytes = settings.max_upload_mb * 1024 * 1024
        named = []
        for f in files:
            data = await f.read(max_bytes + 1)
            named.append((f.filename or "upload.pdf", data))
        result = await run_in_threadpool(ingest, store, named, max_bytes=max_bytes)
        body = result.to_json()
        if workspace_id and result.documents:
            ws = await run_in_threadpool(workspaces.add_documents, workspace_id, [m.id for m in result.documents])
            body["workspace"] = asdict(ws)
        elif workspace_id:
            body["workspace"] = asdict(workspaces.get(workspace_id))
        return body

    @api.get("/documents/{doc_id}", tags=["documents"])
    def document(doc_id: str):
        return doc_summary(get_doc(doc_id))

    @api.delete("/documents/{doc_id}", status_code=204, tags=["documents"])
    def delete_document(doc_id: str):
        store.delete(doc_id)
        workspaces.forget_document(doc_id)
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
    def page_image(doc_id: str, page: int, dpi: int = Query(None, ge=36, le=600),
                   preprocess: str | None = Query(None, description="apply image preprocessing (preview / thumbnails)")):
        """Page render for display. With ``preprocess`` the filters run at this ``dpi`` (its own ``dpi=`` step is
        ignored), which is right for thumbnails; use ``/llm-image`` to see exactly what a model receives."""
        doc, pre = get_doc(doc_id), parse_pre(preprocess)
        dpi = dpi or settings.preview_dpi
        if pre.changes_pixels:
            body, mime = process_bytes(doc.render(page, dpi, "ppm"), pre)
        else:
            body, mime = doc.render(page, dpi), "image/png"
        return Response(body, media_type=mime, headers={"Cache-Control": "max-age=3600"})

    @api.get("/documents/{doc_id}/pages/{page}/llm-image", tags=["pages"])
    def page_llm_image(doc_id: str, page: int, preprocess: str | None = None):
        """The exact image an LLM run sends for this page (LLM_DPI, LLM_MAX_EDGE_PX, PNG/JPEG choice, preprocessing).

        Headers describe it, so the UI can show size and cost-relevant facts without decoding it.
        """
        doc, pre = get_doc(doc_id), parse_pre(preprocess)
        body, mime = services.llm_image(doc, page, pre)
        return Response(body, media_type=mime, headers={
            # percent-encoded: header values are latin-1 and the label may contain × or °
            "Cache-Control": "max-age=600", "X-Preprocess": quote(str(pre)), "X-Preprocess-Label": quote(pre.describe()),
        })

    @api.get("/preprocess", tags=["pages"])
    def check_preprocess(spec: str = ""):
        """Validate and normalise a preprocessing spec (400 with a readable message if it's wrong)."""
        return parse_pre(spec).to_json()

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
        return [services.with_cost(r) for r in get_doc(doc_id).list_llm_results(page)]

    @api.post("/documents/{doc_id}/pages/{page}/llm", tags=["llm"])
    async def run_llm(doc_id: str, page: int, body: LLMRunBody):
        doc = get_doc(doc_id)
        if not 0 <= page < doc.meta.page_count:
            raise DocumentError(f"page {page} out of range")
        try:
            services.resolve_spec(body.spec())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return services.with_cost(await services.run_llm_page(doc, page, body.spec(), force=body.force))

    @api.delete("/documents/{doc_id}/pages/{page}/llm/{key}", status_code=204, tags=["llm"])
    def delete_llm_result(doc_id: str, page: int, key: str):
        doc = get_doc(doc_id)
        folder = (doc.folder / "llm" / str(page)).resolve()
        path = (folder / f"{key}.json").resolve()
        if path.parent != folder or not path.exists():
            raise HTTPException(404, "no such result")
        path.unlink()
        return Response(status_code=204)

    # -------------------------------------------------------------- workspaces
    def ws_json(ws, details: bool = False) -> dict:
        body = asdict(ws)
        metas = [store.get(d).meta for d in ws.doc_ids]
        body["document_count"] = len(metas)
        body["page_count"] = sum(m.page_count for m in metas)
        if details:
            body["documents"] = [doc_summary(store.get(d)) for d in ws.doc_ids]
        return body

    @api.get("/workspaces", tags=["workspaces"])
    def list_workspaces():
        return [ws_json(w) for w in workspaces.list()]

    @api.post("/workspaces", status_code=201, tags=["workspaces"])
    def create_workspace(body: WorkspaceBody):
        return ws_json(workspaces.create(body.name, body.doc_ids))

    @api.post("/workspaces/suggest-names", tags=["workspaces"])
    def suggest_workspace_names(body: SuggestNamesBody):
        """Name ideas in the ``adjective-scientist`` style, led by one taken from the file names if they share one."""
        taken = [w.name for w in workspaces.list()]
        return {"names": suggest_names(body.filenames, taken=taken, count=body.count)}

    @api.get("/workspaces/{ws_id}", tags=["workspaces"])
    def get_workspace(ws_id: str):
        return ws_json(workspaces.get(ws_id), details=True)

    @api.patch("/workspaces/{ws_id}", tags=["workspaces"])
    def rename_workspace(ws_id: str, body: WorkspaceRenameBody):
        return ws_json(workspaces.rename(ws_id, body.name))

    @api.post("/workspaces/{ws_id}/documents", tags=["workspaces"])
    def add_workspace_documents(ws_id: str, body: WorkspaceDocsBody):
        return ws_json(workspaces.add_documents(ws_id, body.doc_ids))

    @api.delete("/workspaces/{ws_id}/documents/{doc_id}", tags=["workspaces"])
    def remove_workspace_document(ws_id: str, doc_id: str):
        """Take a document out of the workspace. The document itself stays in the library."""
        return ws_json(workspaces.remove_document(ws_id, doc_id))

    @api.delete("/workspaces/{ws_id}", status_code=204, tags=["workspaces"])
    def delete_workspace(ws_id: str, delete_documents: bool = False):
        workspaces.delete(ws_id, delete_documents=delete_documents)
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
    def export_source(doc_id: str, source: str = "llm", fmt: Literal["txt", "md", "json", "jsonl"] = "txt",
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
        name = f"{_safe_stem(doc.meta.filename)}.{export.file_tag(source)}.{fmt}"
        body = export.one_source(doc.meta.__dict__, source, services.source_label(doc, source), texts, fmt)
        return Response(body, media_type=export.MEDIA_TYPES[fmt],
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @api.get("/documents/{doc_id}/export/all", tags=["text"])
    def export_all(doc_id: str, fmt: Literal["json", "jsonl", "csv", "md", "zip"] = "json"):
        """Download everything extracted for a document: every source that has at least one page.

        ``json`` = ``{document, sources, pages: [{page, texts: {source_id: text|null}}]}``; ``jsonl`` = one line per
        extracted (page, source); ``csv`` = a row per page, a column per source; ``md`` = sources under each page;
        ``zip`` = the JSON and CSV plus a TXT and Markdown file per source. Nothing is run implicitly.
        """
        doc = get_doc(doc_id)
        data = services.document_texts(doc)
        stem = _safe_stem(doc.meta.filename)
        if fmt == "zip":
            body: str | bytes = export.all_zip(data, stem)
        else:
            body = {"json": export.all_json, "jsonl": export.all_jsonl, "csv": export.all_csv,
                    "md": export.all_md}[fmt](data)
        return Response(body, media_type=export.MEDIA_TYPES[fmt],
                        headers={"Content-Disposition": f'attachment; filename="{stem}.all-sources.{fmt}"'})

    return api


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = DocumentStore(settings.documents_dir)
    providers = build_providers(settings)
    services = Services(settings, providers, build_ocr_engines(settings))
    jobs = JobManager(store, services)
    workspaces = WorkspaceStore(settings.workspaces_dir, store)

    app = FastAPI(title="pyocr-toolkit", version=__version__,
                  description="Inspect and compare PDF/image text extraction: native text, OCR engines and LLMs.")
    app.state.settings = settings
    app.state.store = store
    app.state.providers = providers
    app.state.services = services
    app.state.jobs = jobs
    app.state.workspaces = workspaces

    @app.exception_handler(DocumentError)
    async def _doc_error(request: Request, exc: DocumentError):
        status = 503 if isinstance(exc, OCRUnavailableError) else 404 if str(exc).startswith("unknown ") else 400
        if not request.url.path.startswith("/api"):
            from .web import render_error
            return render_error(request, status, str(exc))
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.exception_handler(ProviderError)
    async def _provider_error(_req, exc: ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    app.include_router(build_api(settings, store, services, jobs, workspaces))
    from .web import build_web

    app.include_router(build_web(settings, store, services, jobs, workspaces))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def __getattr__(name: str):
    # ``uvicorn pyocr_toolkit.api:app`` support without building the app at import time.
    if name == "app":
        return create_app()
    raise AttributeError(name)
