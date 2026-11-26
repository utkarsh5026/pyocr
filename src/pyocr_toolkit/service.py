"""Operations shared by the HTTP API, background jobs and the CLI.

Text *sources* are identified by a string id that is the same on every page
of a document, so a whole document can be read, exported or compared "per
source":

* ``native``       - the file's embedded text layer
* ``ocr``          - Tesseract OCR (kept as the plain id so existing links,
                     exports and caches keep working)
* ``ocr:<engine>`` - another OCR engine (``ocr:easyocr``, ``ocr:google_vision``,
                     ...); ``ocr:tesseract`` is accepted as an alias of ``ocr``
* ``llm:<key>``    - one LLM configuration (provider + model + prompt); the key
                     is derived from those three, so it is identical across pages
* ``llm``          - shorthand for "the most recent LLM result on each page"
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pymupdf

from .config import Settings
from .documents import DEFAULT_OCR_ENGINE, DocumentError, OpenDocument, TextLayer
from .llm import PROMPT_MODES, USER_PROMPT, Provider, ProviderError, TranscriptionRequest, resolve_prompt
from .ocr import OCREngine, build_ocr_engines


def ocr_engine_of(source: str) -> str | None:
    """``ocr`` -> ``tesseract``, ``ocr:<id>`` -> ``<id>``, anything else -> ``None``."""
    if source == "ocr":
        return DEFAULT_OCR_ENGINE
    if source.startswith("ocr:"):
        return source[4:] or DEFAULT_OCR_ENGINE
    return None


def ocr_source_id(engine: str) -> str:
    return "ocr" if engine == DEFAULT_OCR_ENGINE else f"ocr:{engine}"


@dataclass
class LLMSpec:
    provider: str
    model: str | None = None
    prompt_mode: str = "verbatim"
    custom_prompt: str | None = None


class Services:
    def __init__(self, settings: Settings, providers: dict[str, Provider],
                 ocr_engines: dict[str, OCREngine] | None = None) -> None:
        self.settings = settings
        self.providers = providers
        self.ocr_engines = ocr_engines if ocr_engines is not None else build_ocr_engines(settings)
        self.llm_semaphore = asyncio.Semaphore(settings.llm_concurrency)

    # ----------------------------------------------------------------- LLM
    def resolve_spec(self, spec: LLMSpec) -> tuple[Provider, str, str]:
        """Return (provider, model, system_prompt) or raise ValueError."""
        provider = self.providers.get(spec.provider)
        if provider is None:
            raise ValueError(f"unknown provider '{spec.provider}'")
        system_prompt = resolve_prompt(spec.prompt_mode, spec.custom_prompt)
        model = (spec.model or provider.default_model or "").strip()
        return provider, model, system_prompt

    def llm_key(self, doc: OpenDocument, spec: LLMSpec) -> str:
        provider, model, system_prompt = self.resolve_spec(spec)
        return doc.llm_cache_key(provider.id, model, system_prompt)

    async def run_llm_page(self, doc: OpenDocument, page: int, spec: LLMSpec, force: bool = False) -> dict[str, Any]:
        provider, model, system_prompt = self.resolve_spec(spec)
        key = doc.llm_cache_key(provider.id, model, system_prompt)
        if not force:
            cached = doc.load_llm_result(page, key)
            if cached:
                return {**cached, "key": key, "cached": True}

        s = self.settings
        # PNG keeps text edges crisp; fall back to JPEG for huge colour pages
        # (photos / colour scans) to stay inside provider upload limits.
        image = await asyncio.to_thread(doc.render, page, s.llm_dpi, "png", s.llm_max_edge_px)
        mime = "image/png"
        if len(image) > 3_500_000:
            image = await asyncio.to_thread(doc.render, page, s.llm_dpi, "jpeg", s.llm_max_edge_px)
            mime = "image/jpeg"

        async with self.llm_semaphore:
            result = await provider.transcribe(TranscriptionRequest(
                image=image, mime_type=mime, system_prompt=system_prompt,
                user_prompt=USER_PROMPT, model=model,
            ))
        record = {
            "provider": provider.id,
            "provider_label": provider.label,
            "model": result.model,
            "requested_model": model,
            "prompt_mode": spec.prompt_mode,
            "prompt": system_prompt if spec.prompt_mode == "custom" else None,
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

    async def test_provider(self, provider_id: str, model: str | None) -> dict[str, Any]:
        """Send a tiny generated image and report what came back."""
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError(f"unknown provider '{provider_id}'")
        expected = "pyocr test 4821"
        tmp = pymupdf.open()
        page = tmp.new_page(width=300, height=80)
        page.insert_text((20, 50), expected, fontsize=22)
        png = page.get_pixmap(dpi=150).tobytes("png")
        result = await provider.transcribe(TranscriptionRequest(
            image=png, mime_type="image/png", system_prompt=PROMPT_MODES["verbatim"]["system"],
            user_prompt=USER_PROMPT, model=model, max_output_tokens=2000,
        ))
        return {
            "ok": expected in result.text,
            "expected": expected,
            "text": result.text,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "warnings": result.warnings,
        }

    # ----------------------------------------------------------------- OCR
    def ocr_engine(self, engine_id: str | None) -> OCREngine:
        engine = self.ocr_engines.get(engine_id or DEFAULT_OCR_ENGINE)
        if engine is None:
            raise DocumentError(f"unknown OCR engine '{engine_id}' (known: {', '.join(self.ocr_engines)})")
        return engine

    def ocr_label(self, engine_id: str) -> str:
        engine = self.ocr_engines.get(engine_id)
        return engine.label if engine else engine_id

    def run_ocr_page(self, doc: OpenDocument, page: int, force: bool = False,
                     engine: str = DEFAULT_OCR_ENGINE) -> TextLayer:
        """OCR one page with one engine (cached per engine). Blocking: use a thread from async code."""
        return self.ocr_engine(engine).extract(doc, page, force=force)

    async def test_ocr_engine(self, engine_id: str) -> dict[str, Any]:
        engine = self.ocr_engine(engine_id)
        return await asyncio.to_thread(engine.test)

    # ----------------------------------------------------------------- sources
    def list_sources(self, doc: OpenDocument) -> list[dict[str, Any]]:
        """All text sources available for a document, with page coverage.

        Tesseract is always listed (as before); other OCR engines only once
        they have a result on at least one page.
        """
        n = doc.meta.page_count
        ocr_pages: dict[str, list[int]] = {DEFAULT_OCR_ENGINE: []}
        for p in range(n):
            for engine in doc.ocr_engines_cached(p):
                ocr_pages.setdefault(engine, []).append(p)
        sources = [{"id": "native", "kind": "native", "label": "Native text layer", "pages": list(range(n))}]
        order = list(self.ocr_engines)
        for engine in sorted(ocr_pages, key=lambda e: order.index(e) if e in order else len(order)):
            known = self.ocr_engines.get(engine)
            sources.append({"id": ocr_source_id(engine), "kind": "ocr", "engine": engine,
                            "label": self.ocr_label(engine), "brand": known.brand if known else "",
                            "pages": ocr_pages[engine]})
        llm: dict[str, dict[str, Any]] = {}
        for p in range(n):
            for r in doc.list_llm_results(p):
                entry = llm.setdefault(r["key"], {
                    "id": f"llm:{r['key']}", "kind": "llm", "key": r["key"],
                    "brand": getattr(self.providers.get(r["provider"]), "brand", ""),
                    "provider": r["provider"], "model": r["model"], "prompt_mode": r.get("prompt_mode"),
                    "label": f"{r.get('provider_label') or r['provider']} · {r['model']} · {r.get('prompt_mode')}",
                    "pages": [], "input_tokens": 0, "output_tokens": 0, "latency_ms": 0.0,
                    "updated_at": 0.0,
                })
                entry["pages"].append(p)
                entry["input_tokens"] += r.get("input_tokens") or 0
                entry["output_tokens"] += r.get("output_tokens") or 0
                entry["latency_ms"] += r.get("latency_ms") or 0
                entry["updated_at"] = max(entry["updated_at"], r.get("created_at") or 0)
        sources.extend(sorted(llm.values(), key=lambda e: e["updated_at"], reverse=True))
        for s in sources:
            s["page_count"] = len(s["pages"])
        return sources

    def page_text(self, doc: OpenDocument, page: int, source: str, run_ocr: bool = False) -> str | None:
        """Text of one page from one source, or ``None`` if not extracted yet."""
        if source == "native":
            return doc.native_text(page).text
        if (engine := ocr_engine_of(source)) is not None:
            layer = self.run_ocr_page(doc, page, engine=engine) if run_ocr else doc.cached_ocr_text(page, engine)
            return layer.text if layer else None
        if source == "llm" or source.startswith("llm:"):
            key = source[4:] if source.startswith("llm:") else None
            results = doc.list_llm_results(page)
            if key:
                results = [r for r in results if r["key"] == key]
            return results[0]["text"] if results else None
        raise DocumentError(f"unknown source '{source}'")

    def source_label(self, doc: OpenDocument, source: str) -> str:
        if (engine := ocr_engine_of(source)) is not None:
            return self.ocr_label(engine)
        for s in self.list_sources(doc):
            if s["id"] == source:
                return s["label"]
        return {"llm": "Latest LLM transcription"}.get(source, source)


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """'1-3,5' -> [0, 1, 2, 4]. Input is 1-based, output 0-based; empty -> all pages."""
    if not spec or not spec.strip() or spec.strip().lower() == "all":
        return list(range(page_count))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                pages.update(range(int(a), int(b) + 1))
            else:
                pages.add(int(part))
        except ValueError as exc:
            raise ValueError(f"bad page range '{part}' (use e.g. 1-3,7)") from exc
    out = sorted(p - 1 for p in pages if 1 <= p <= page_count)
    if not out:
        raise ValueError("page range selects no pages")
    return out


__all__ = ["LLMSpec", "ProviderError", "Services", "ocr_engine_of", "ocr_source_id", "parse_pages"]
