"""Operations shared by the HTTP API, background jobs and the CLI.

Text *sources* are identified by a string id that is the same on every page
of a document, so a whole document can be read, exported or compared "per
source":

* ``native``       - the file's embedded text layer
* ``ocr``          - Tesseract OCR
* ``llm:<key>``    - one LLM configuration (provider + model + prompt); the key
                     is derived from those three, so it is identical across pages
* ``llm``          - shorthand for "the most recent LLM result on each page"

An LLM configuration may include image preprocessing (grayscale, contrast,
binarize, ...; see :mod:`pyocr_toolkit.preprocess`). It is part of the cache
key, so "same model on the original page" and "same model on the cleaned-up
page" are two sources you can compare against each other.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pymupdf

from .config import Settings
from .credentials import CredentialStore
from .documents import DocumentError, OpenDocument
from .llm import PROMPT_MODES, USER_PROMPT, Provider, ProviderError, TranscriptionRequest, catalog, resolve_prompt
from .preprocess import MAX_PNG_BYTES, Preprocess, process_bytes


@dataclass
class LLMSpec:
    provider: str
    model: str | None = None
    prompt_mode: str = "verbatim"
    custom_prompt: str | None = None
    #: image preprocessing spec, e.g. ``"gray;contrast=1.5"`` or a preset id; empty = the page as rendered
    preprocess: str = ""


class Services:
    def __init__(self, settings: Settings, providers: dict[str, Provider]) -> None:
        self.settings = settings
        self.providers = providers
        self.llm_semaphore = asyncio.Semaphore(settings.llm_concurrency)
        # Values saved on the Providers page override env vars; applied here so the
        # web app and the CLI see the same configuration.
        self.credentials = CredentialStore(settings, providers)

    # ----------------------------------------------------------------- LLM
    def resolve_spec(self, spec: LLMSpec) -> tuple[Provider, str, str]:
        """Return (provider, model, system_prompt) or raise ValueError (also for a bad preprocess spec)."""
        provider = self.providers.get(spec.provider)
        if provider is None:
            raise ValueError(f"unknown provider '{spec.provider}'")
        system_prompt = resolve_prompt(spec.prompt_mode, spec.custom_prompt)
        model = (spec.model or provider.default_model or "").strip()
        Preprocess.parse(spec.preprocess)
        return provider, model, system_prompt

    def llm_key(self, doc: OpenDocument, spec: LLMSpec) -> str:
        provider, model, system_prompt = self.resolve_spec(spec)
        return doc.llm_cache_key(provider.id, model, system_prompt, str(Preprocess.parse(spec.preprocess)))

    def llm_image(self, doc: OpenDocument, page: int, pre: Preprocess | None = None) -> tuple[bytes, str]:
        """The exact image (bytes, mime type) an LLM receives for this page.

        Used both to run the model and to preview "what the model sees", so the
        two can never drift apart. Blocking: call it from a thread in async code.
        """
        s, pre = self.settings, pre or Preprocess()
        dpi = pre.dpi or s.llm_dpi
        if pre.changes_pixels:
            raw = doc.render(page, dpi, "ppm", s.llm_max_edge_px)
            return process_bytes(raw, pre)
        # PNG keeps text edges crisp; fall back to JPEG for huge colour pages
        # (photos / colour scans) to stay inside provider upload limits.
        image = doc.render(page, dpi, "png", s.llm_max_edge_px)
        if len(image) > MAX_PNG_BYTES:
            return doc.render(page, dpi, "jpeg", s.llm_max_edge_px), "image/jpeg"
        return image, "image/png"

    def cost(self, provider_id: str, model: str | None, input_tokens: int | None,
             output_tokens: int | None) -> dict[str, Any] | None:
        """Estimated cost of one call at catalog list prices, in USD and INR.

        None when the model has no catalog price. List prices only: no taxes (e.g.
        GST), batch or prompt-caching discounts, and a self-hosted model is free.
        Computed on read, never stored, so a price update applies to old results.
        """
        found = catalog.price(model or "", provider_id)
        if found is None or input_tokens is None:
            return None
        p_in, p_out, match = found
        usd = ((input_tokens or 0) * p_in + (output_tokens or 0) * p_out) / 1_000_000
        rate = self.settings.usd_inr_rate
        return {"usd": usd, "inr": usd * rate, "usd_inr_rate": rate, "price_in": p_in, "price_out": p_out,
                "price_model": match}

    def with_cost(self, result: dict[str, Any]) -> dict[str, Any]:
        """A stored LLM result plus its ``cost`` (see :meth:`cost`)."""
        model = result.get("model") or result.get("requested_model")
        cost = self.cost(result.get("provider", ""), model, result.get("input_tokens"), result.get("output_tokens"))
        if cost is None and result.get("requested_model") and result.get("requested_model") != model:
            cost = self.cost(result.get("provider", ""), result["requested_model"],
                             result.get("input_tokens"), result.get("output_tokens"))
        return {**result, "cost": cost}

    async def run_llm_page(self, doc: OpenDocument, page: int, spec: LLMSpec, force: bool = False,
                           on_start: Callable[[], None] | None = None) -> dict[str, Any]:
        """Transcribe one page (or return its cached result).

        ``on_start`` is called once this page holds an ``llm_semaphore`` slot, just
        before the provider call. Jobs use it to tell "waiting for a slot" apart
        from "the model is reading this page"; it isn't called for cache hits.
        """
        provider, model, system_prompt = self.resolve_spec(spec)
        pre = Preprocess.parse(spec.preprocess)
        key = doc.llm_cache_key(provider.id, model, system_prompt, str(pre))
        if not force:
            cached = doc.load_llm_result(page, key)
            if cached:
                return {**cached, "key": key, "cached": True}

        image, mime = await asyncio.to_thread(self.llm_image, doc, page, pre)

        async with self.llm_semaphore:
            if on_start:
                on_start()
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
            # canonical spec + readable label; absent/empty = the page as rendered
            "preprocess": str(pre) or None,
            "preprocess_label": pre.describe() if not pre.is_identity else None,
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

    async def provider_models(self, provider_id: str, live: bool = True) -> dict[str, Any]:
        """Catalog models for a provider, plus (if possible) what its API says this key can use.

        Every name is annotated with :func:`catalog.lookup`, so the UI can say whether it suits
        transcription. A failed live listing is reported, not raised: the catalog still helps.
        """
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError(f"unknown provider '{provider_id}'")
        out: dict[str, Any] = {
            "provider": provider.id,
            "default_model": provider.default_model,
            "catalog": [m.to_json() for m in catalog.search(provider_id=provider.id)],
            "live": None,
            "live_supported": provider.can_list_models,
            "live_error": None,
        }
        if live and out["live_supported"]:
            if not provider.configured:
                out["live_error"] = f"{provider.label} is not configured; set {', '.join(provider.missing_settings())}"
            else:
                try:
                    names = await provider.list_models()
                except ProviderError as exc:
                    out["live_error"] = str(exc)
                else:
                    out["live"] = [catalog.lookup(n, provider.id) for n in names or []]
        return out

    # ----------------------------------------------------------------- OCR
    def run_ocr_page(self, doc: OpenDocument, page: int, force: bool = False):
        return doc.ocr_text(page, dpi=self.settings.ocr_dpi, language=self.settings.tesseract_language, force=force)

    # ----------------------------------------------------------------- sources
    @staticmethod
    def list_sources(doc: OpenDocument) -> list[dict[str, Any]]:
        """All text sources available for a document, with page coverage."""
        n = doc.meta.page_count
        ocr_pages = [p for p in range(n) if doc.cached_ocr_text(p) is not None]
        sources = [
            {"id": "native", "kind": "native", "label": "Native text layer", "pages": list(range(n))},
            {"id": "ocr", "kind": "ocr", "label": "Tesseract OCR", "pages": ocr_pages},
        ]
        llm: dict[str, dict[str, Any]] = {}
        for p in range(n):
            for r in doc.list_llm_results(p):
                entry = llm.setdefault(r["key"], {
                    "id": f"llm:{r['key']}", "kind": "llm", "key": r["key"],
                    "provider": r["provider"], "model": r["model"], "prompt_mode": r.get("prompt_mode"),
                    "preprocess": r.get("preprocess"),
                    "label": f"{r.get('provider_label') or r['provider']} · {r['model']} · {r.get('prompt_mode')}"
                             + (f" · image: {r['preprocess_label']}" if r.get("preprocess_label") else ""),
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
        if source == "ocr":
            layer = self.run_ocr_page(doc, page) if run_ocr else doc.cached_ocr_text(page)
            return layer.text if layer else None
        if source == "llm" or source.startswith("llm:"):
            key = source[4:] if source.startswith("llm:") else None
            results = doc.list_llm_results(page)
            if key:
                results = [r for r in results if r["key"] == key]
            return results[0]["text"] if results else None
        raise DocumentError(f"unknown source '{source}'")

    def source_label(self, doc: OpenDocument, source: str) -> str:
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


__all__ = ["LLMSpec", "ProviderError", "Services", "parse_pages"]
