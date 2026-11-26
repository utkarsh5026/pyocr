"""HTTP API + static UI.

Run with ``pyocr serve`` (or ``uvicorn pyocr.api:app``) and open http://localhost:8000.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import compare, search
from .config import Settings, get_settings
from .documents import DocumentError, DocumentStore, OCRUnavailableError, OpenDocument
from .llm import (
    PROMPT_MODES,
    USER_PROMPT,
    ProviderError,
    TranscriptionRequest,
    build_providers,
    resolve_prompt,
)

STATIC_DIR = Path(__file__).parent / "static"
TextSource = Literal["native", "ocr", "llm"]


class LLMRunBody(BaseModel):
    provider: str
    model: str | None = None
    prompt_mode: str = "verbatim"
    custom_prompt: str | None = None
    force: bool = False


class TextRef(BaseModel):
    source: TextSource
    #: for ``llm``: cache key of a stored result (``None`` -> most recent one)
    key: str | None = None


class CompareBody(BaseModel):
    reference: TextRef = Field(default_factory=lambda: TextRef(source="llm"))
    hypothesis: TextRef = Field(default_factory=lambda: TextRef(source="ocr"))
    ignore_case: bool = False
    ignore_punctuation: bool = False
    diff: bool = True


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = DocumentStore(settings.documents_dir)
    providers = build_providers(settings)
    llm_semaphore = asyncio.Semaphore(settings.llm_concurrency)

    app = FastAPI(title="pyocr - OCR / LLM extraction inspector", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.providers = providers

    @app.exception_handler(DocumentError)
    async def _doc_error(_req, exc: DocumentError):
        status = 503 if isinstance(exc, OCRUnavailableError) else 404 if "unknown document" in str(exc) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.exception_handler(ProviderError)
    async def _provider_error(_req, exc: ProviderError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    # ------------------------------------------------------------------ helpers
    def get_doc(doc_id: str) -> OpenDocument:
        return store.get(doc_id)

    def resolve_text(doc: OpenDocument, page: int, ref: TextRef) -> tuple[str, str]:
        """Return (text, label) for a text source on a page."""
        if ref.source == "native":
            return doc.native_text(page).text, "Native text layer"
        if ref.source == "ocr":
            layer = doc.ocr_text(page, dpi=settings.ocr_dpi, language=settings.tesseract_language)
            return layer.text, "Tesseract OCR"
        results = doc.list_llm_results(page)
        if ref.key:
            results = [r for r in results if r["key"] == ref.key]
        if not results:
            raise DocumentError(f"no LLM transcription for page {page + 1} yet - run one first")
        r = results[0]
        return r["text"], f"LLM: {r['provider']} / {r['model']}"

    # ------------------------------------------------------------------ config
    @app.get("/api/config")
    def config():
        return {
            "providers": [p.describe() for p in providers.values()],
            "prompt_modes": [{"id": k, "label": v["label"], "prompt": v["system"]} for k, v in PROMPT_MODES.items()],
            "tesseract_available": shutil.which("tesseract") is not None,
            "tesseract_language": settings.tesseract_language,
            "max_upload_mb": settings.max_upload_mb,
            "preview_dpi": settings.preview_dpi,
        }

    # --------------------------------------------------------------- documents
    @app.get("/api/documents")
    def list_documents():
        return [m.__dict__ for m in store.list()]

    @app.post("/api/documents", status_code=201)
    async def upload(file: UploadFile = File(...)):
        data = await file.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"file larger than {settings.max_upload_mb} MB")
        meta = await run_in_threadpool(store.add, file.filename or "upload.pdf", data)
        return meta.__dict__

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: str):
        return get_doc(doc_id).meta.__dict__

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: str):
        store.delete(doc_id)
        return Response(status_code=204)

    @app.get("/api/documents/{doc_id}/source")
    def source(doc_id: str):
        doc = get_doc(doc_id)
        return FileResponse(doc.source_path, filename=doc.meta.filename)

    @app.get("/api/documents/{doc_id}/pages")
    def pages(doc_id: str):
        doc = get_doc(doc_id)
        return [doc.page_info(n) for n in doc.iter_pages()]

    @app.get("/api/documents/{doc_id}/pages/{page}/image")
    def page_image(doc_id: str, page: int, dpi: int = Query(None, ge=36, le=600)):
        doc = get_doc(doc_id)
        png = doc.render(page, dpi or settings.preview_dpi)
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    # ------------------------------------------------------------- text layers
    @app.get("/api/documents/{doc_id}/pages/{page}/text/native")
    def native_text(doc_id: str, page: int):
        return get_doc(doc_id).native_text(page).to_json()

    @app.get("/api/documents/{doc_id}/pages/{page}/text/ocr")
    def ocr_text(doc_id: str, page: int, force: bool = False, cached_only: bool = False):
        doc = get_doc(doc_id)
        if cached_only:
            layer = doc.cached_ocr_text(page)
            return layer.to_json() if layer else None
        layer = doc.ocr_text(page, dpi=settings.ocr_dpi, language=settings.tesseract_language, force=force)
        return layer.to_json()

    # --------------------------------------------------------------------- LLM
    @app.get("/api/documents/{doc_id}/pages/{page}/llm")
    def llm_results(doc_id: str, page: int):
        return get_doc(doc_id).list_llm_results(page)

    @app.post("/api/documents/{doc_id}/pages/{page}/llm")
    async def run_llm(doc_id: str, page: int, body: LLMRunBody):
        doc = get_doc(doc_id)
        provider = providers.get(body.provider)
        if provider is None:
            raise HTTPException(400, f"unknown provider '{body.provider}'")
        try:
            system_prompt = resolve_prompt(body.prompt_mode, body.custom_prompt)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        model = (body.model or provider.default_model or "").strip()
        key = doc.llm_cache_key(provider.id, model, system_prompt)
        if not body.force:
            cached = doc.load_llm_result(page, key)
            if cached:
                return {**cached, "key": key, "cached": True}

        # PNG keeps text edges crisp; fall back to JPEG if the page is huge
        # (photos / colour scans) to stay inside provider upload limits.
        image = await run_in_threadpool(
            doc.render, page, settings.llm_dpi, "png", settings.llm_max_edge_px
        )
        mime = "image/png"
        if len(image) > 3_500_000:
            image = await run_in_threadpool(
                doc.render, page, settings.llm_dpi, "jpeg", settings.llm_max_edge_px
            )
            mime = "image/jpeg"

        async with llm_semaphore:
            result = await provider.transcribe(TranscriptionRequest(
                image=image, mime_type=mime, system_prompt=system_prompt,
                user_prompt=USER_PROMPT, model=model,
            ))
        record = {
            "provider": provider.id,
            "provider_label": provider.label,
            "model": result.model,
            "requested_model": model,
            "prompt_mode": body.prompt_mode,
            "prompt": system_prompt if body.prompt_mode == "custom" else None,
            "text": result.text,
            "latency_ms": result.latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "stop_reason": result.stop_reason,
            "warnings": result.warnings,
            "image_bytes": len(image),
            "image_mime": mime,
            "created_at": time.time(),
        }
        doc.save_llm_result(page, key, record)
        return {**record, "key": key, "cached": False}

    @app.delete("/api/documents/{doc_id}/pages/{page}/llm/{key}", status_code=204)
    def delete_llm_result(doc_id: str, page: int, key: str):
        doc = get_doc(doc_id)
        path = doc.folder / "llm" / str(page) / f"{key}.json"
        if path.resolve().parent != (doc.folder / "llm" / str(page)).resolve() or not path.exists():
            raise HTTPException(404, "no such result")
        path.unlink()
        return Response(status_code=204)

    # ----------------------------------------------------------------- compare
    @app.post("/api/documents/{doc_id}/pages/{page}/compare")
    def compare_page(doc_id: str, page: int, body: CompareBody):
        doc = get_doc(doc_id)
        ref_text, ref_label = resolve_text(doc, page, body.reference)
        hyp_text, hyp_label = resolve_text(doc, page, body.hypothesis)
        opts = compare.NormalizeOptions(body.ignore_case, body.ignore_punctuation)
        return {
            "reference": {"label": ref_label, **body.reference.model_dump()},
            "hypothesis": {"label": hyp_label, **body.hypothesis.model_dump()},
            "metrics": compare.score(ref_text, hyp_text, opts),
            "diff": compare.word_diff(ref_text, hyp_text, opts) if body.diff else None,
        }

    @app.get("/api/documents/{doc_id}/report")
    def report(doc_id: str, ignore_case: bool = True, ignore_punctuation: bool = False):
        """Per-page overview: what has been extracted and how well engines agree.

        Agreement is measured against the most recent LLM transcription of each
        page (when one exists), using only already-computed OCR results.
        """
        doc = get_doc(doc_id)
        opts = compare.NormalizeOptions(ignore_case, ignore_punctuation)
        rows = []
        for n in doc.iter_pages():
            native = doc.native_text(n).text
            ocr = doc.cached_ocr_text(n)
            llm = doc.list_llm_results(n)
            row = {
                "page": n,
                "native_chars": len(native.strip()),
                "ocr_chars": len(ocr.text.strip()) if ocr else None,
                "llm_runs": len(llm),
                "llm_model": f"{llm[0]['provider']} / {llm[0]['model']}" if llm else None,
                "native_vs_llm": None,
                "ocr_vs_llm": None,
                "native_vs_ocr": None,
            }
            if llm:
                ref = llm[0]["text"]
                if native.strip():
                    row["native_vs_llm"] = compare.score(ref, native, opts)
                if ocr:
                    row["ocr_vs_llm"] = compare.score(ref, ocr.text, opts)
            if ocr and native.strip():
                row["native_vs_ocr"] = compare.score(native, ocr.text, opts)
            rows.append(row)
        return {"document": doc.meta.__dict__, "pages": rows}

    # ------------------------------------------------------------------ search
    @app.get("/api/documents/{doc_id}/search")
    def search_document(
        doc_id: str,
        q: str = Query(..., min_length=1),
        sources: str = "native,ocr,llm",
        limit_per_page: int = Query(10, ge=1, le=100),
    ):
        """Search every page. OCR is only searched on pages already OCR'd (it's slow)."""
        doc = get_doc(doc_id)
        wanted = {s.strip() for s in sources.split(",") if s.strip()}
        pages_out = []
        total = 0
        ocr_missing = 0
        for n in doc.iter_pages():
            hits = []
            if "native" in wanted:
                layer = doc.native_text(n)
                count = search.count_matches(layer.text, q)
                if count:
                    hits.append({
                        "source": "native", "label": "Native text", "count": count,
                        "boxes": search.find_boxes(layer.words, q),
                        "snippets": search.find_snippets(layer.text, q, limit=limit_per_page),
                    })
            if "ocr" in wanted:
                layer = doc.cached_ocr_text(n)
                if layer is None:
                    ocr_missing += 1
                else:
                    count = search.count_matches(layer.text, q)
                    if count:
                        hits.append({
                            "source": "ocr", "label": "Tesseract OCR", "count": count,
                            "boxes": search.find_boxes(layer.words, q),
                            "snippets": search.find_snippets(layer.text, q, limit=limit_per_page),
                        })
            if "llm" in wanted:
                for r in doc.list_llm_results(n):
                    count = search.count_matches(r["text"], q)
                    if count:
                        hits.append({
                            "source": "llm", "key": r["key"], "label": f"LLM: {r['provider']} / {r['model']}",
                            "count": count, "boxes": [],
                            "snippets": search.find_snippets(r["text"], q, limit=limit_per_page),
                        })
            if hits:
                total += sum(h["count"] for h in hits)
                pages_out.append({"page": n, "hits": hits})
        return {"query": q, "total": total, "pages": pages_out, "pages_without_ocr": ocr_missing}

    # ------------------------------------------------------------------ export
    @app.get("/api/documents/{doc_id}/export")
    def export(doc_id: str, source: TextSource = "llm", fmt: Literal["txt", "json"] = "txt",
               key: str | None = None):
        """Download the whole document's text from one source.

        Pages without a result for that source are left empty (OCR and LLM are
        not run implicitly here).
        """
        doc = get_doc(doc_id)
        pages_text: list[str] = []
        for n in doc.iter_pages():
            if source == "native":
                pages_text.append(doc.native_text(n).text)
            elif source == "ocr":
                layer = doc.cached_ocr_text(n)
                pages_text.append(layer.text if layer else "")
            else:
                results = doc.list_llm_results(n)
                if key:
                    results = [r for r in results if r["key"] == key] or results
                pages_text.append(results[0]["text"] if results else "")
        # ASCII-only filename: HTTP headers are latin-1 and quotes would break them.
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(doc.meta.filename).stem) or "document"
        if fmt == "json":
            return JSONResponse(
                {"document": doc.meta.__dict__, "source": source,
                 "pages": [{"page": i + 1, "text": t} for i, t in enumerate(pages_text)]},
                headers={"Content-Disposition": f'attachment; filename="{stem}.{source}.json"'},
            )
        body = "".join(f"\n\n===== Page {i + 1} =====\n\n{t.strip()}" for i, t in enumerate(pages_text)).lstrip()
        return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{stem}.{source}.txt"'})

    # --------------------------------------------------------------------- UI
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


def __getattr__(name: str):
    # ``uvicorn pyocr.api:app`` support without building the app at import time.
    if name == "app":
        return create_app()
    raise AttributeError(name)
