"""Operations shared by the HTTP API, background jobs and the CLI.

Text *sources* are identified by a string id that is the same on every page
of a document, so a whole document can be read, exported or compared "per
source":

* ``native``       - the file's embedded text layer, read by PyMuPDF
* ``native:<lib>`` - the same text layer read by another PDF library (``native:pypdf``,
                     ``native:pdfplumber``, ...; see :mod:`pyocr_toolkit.pdftext`)
* ``ocr``          - Tesseract OCR (kept as the plain id so existing links,
                     exports and caches keep working)
* ``ocr:<engine>`` - another OCR engine (``ocr:easyocr``, ``ocr:google_vision``,
                     ...); ``ocr:tesseract`` is accepted as an alias of ``ocr``
* ``llm:<key>``    - one LLM configuration (provider + model + prompt); the key
                     is derived from those three, so it is identical across pages
* ``llm``          - shorthand for "the most recent LLM result on each page"

An LLM configuration may include image preprocessing (grayscale, contrast,
binarize, ...; see :mod:`pyocr_toolkit.preprocess`). It is part of the cache
key, so "same model on the original page" and "same model on the cleaned-up
page" are two sources you can compare against each other.

LLM pages can also be sent several to a request (``LLMSpec.pages_per_request``),
the way many production pipelines batch them. The reply is split back into pages
by marker lines, and every page is still cached on its own, so a batched run is
just another ``llm:<key>`` source: read, export and compare it like any other,
including against the same model run one page at a time.

The ``extract`` prompt mode fills a JSON Schema instead of transcribing (see
:mod:`pyocr_toolkit.extract`). The schema is part of the cache key too, and the
parsed ``data`` plus its validation errors are stored next to the raw reply.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pymupdf

from . import compare, extract
from .config import Settings
from .credentials import CredentialStore
from .documents import DEFAULT_OCR_ENGINE, DocumentError, OpenDocument, TextLayer
from .export import DocumentTexts, SourceTexts
from .llm import (
    BATCH_USER_PROMPT,
    DRAFT_SYSTEM,
    EXTRACT_MODE,
    EXTRACT_USER_PROMPT,
    MAX_PAGES_PER_REQUEST,
    PROMPT_MODES,
    USER_PROMPT,
    PageImage,
    Provider,
    ProviderError,
    TranscriptionRequest,
    TranscriptionResult,
    batch_rules,
    catalog,
    draft_user_prompt,
    resolve_prompt,
    split_batch,
)
from .ocr import OCREngine, build_ocr_engines
from .pdftext import DEFAULT_TEXT_LIBRARY, TextLibrary, build_text_libraries, native_library_of
from .preprocess import MAX_PNG_BYTES, Preprocess, process_bytes

#: stop reasons that mean "the output limit cut the reply short" (Claude, OpenAI, Gemini)
_TRUNCATED = {"max_tokens", "length", "MAX_TOKENS"}


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
    #: image preprocessing spec, e.g. ``"gray;contrast=1.5"`` or a preset id; empty = the page as rendered
    preprocess: str = ""
    #: pages sent together in one request; 1 = one page per request (the default)
    pages_per_request: int = 1
    #: for ``prompt_mode="extract"``: a preset id (``invoice``, ...) or JSON Schema text
    extract_schema: str = ""


@dataclass
class ExtractSchema:
    schema: dict[str, Any]
    label: str
    preset: str | None

    @property
    def canonical(self) -> str:
        return extract.canonical(self.schema)


#: Source id of the text a person confirmed on the Compare page's Review panel.
CORRECTED_SOURCE = "corrected"
CORRECTED_LABEL = "Corrected (reviewed)"

class Services:
    def __init__(self, settings: Settings, providers: dict[str, Provider],
                 ocr_engines: dict[str, OCREngine] | None = None,
                 text_libraries: dict[str, TextLibrary] | None = None) -> None:
        self.settings = settings
        self.providers = providers
        self.ocr_engines = ocr_engines if ocr_engines is not None else build_ocr_engines(settings)
        self.text_libraries = text_libraries if text_libraries is not None else build_text_libraries()
        self.llm_semaphore = asyncio.Semaphore(settings.llm_concurrency)
        # Values saved on the Providers page override env vars; applied here so the
        # web app and the CLI see the same configuration.
        self.credentials = CredentialStore(settings, providers, self.ocr_engines)

    # ----------------------------------------------------------------- LLM
    def resolve_spec(self, spec: LLMSpec) -> tuple[Provider, str, str]:
        """Return (provider, model, system_prompt) or raise ValueError (also for a bad preprocess spec)."""
        provider = self.providers.get(spec.provider)
        if provider is None:
            raise ValueError(f"unknown provider '{spec.provider}'")
        if not 1 <= spec.pages_per_request <= MAX_PAGES_PER_REQUEST:
            raise ValueError(f"pages per request must be between 1 and {MAX_PAGES_PER_REQUEST}")
        if spec.prompt_mode == EXTRACT_MODE and spec.pages_per_request > 1:
            # a batched reply is split into pages by marker lines; an extraction reply is one JSON object per page
            raise ValueError("structured data extraction runs one page per request; set pages per request to 1")
        system_prompt = resolve_prompt(spec.prompt_mode, spec.custom_prompt)
        if spec.pages_per_request > 1:
            # part of the prompt, so part of the cache key: a batched run is its own source
            system_prompt += batch_rules(spec.pages_per_request)
        model = (spec.model or provider.default_model or "").strip()
        Preprocess.parse(spec.preprocess)
        self.resolve_schema(spec)
        return provider, model, system_prompt

    @staticmethod
    def resolve_schema(spec: LLMSpec) -> ExtractSchema | None:
        """The extraction schema of an ``extract`` spec (None otherwise); ValueError if it's unusable."""
        if spec.prompt_mode != EXTRACT_MODE:
            return None
        schema, label, preset = extract.resolve(spec.extract_schema)
        return ExtractSchema(schema, label, preset)

    def llm_key(self, doc: OpenDocument, spec: LLMSpec) -> str:
        provider, model, system_prompt = self.resolve_spec(spec)
        schema = self.resolve_schema(spec)
        return doc.llm_cache_key(provider.id, model, system_prompt, str(Preprocess.parse(spec.preprocess)),
                                 schema.canonical if schema else "")

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

        With a batched spec the page goes out as a batch of one, so it lands in the
        same source as the rest of that run.
        """
        if spec.pages_per_request > 1:
            outcome = (await self.run_llm_batch(doc, [page], spec, force=force, on_start=on_start))[page]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        provider, model, system_prompt = self.resolve_spec(spec)
        pre = Preprocess.parse(spec.preprocess)
        schema = self.resolve_schema(spec)
        key = doc.llm_cache_key(provider.id, model, system_prompt, str(pre), schema.canonical if schema else "")
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
                user_prompt=EXTRACT_USER_PROMPT if schema else USER_PROMPT, model=model,
                output_schema=extract.provider_schema(schema.schema) if schema else None,
            ))
        text = result.text
        structured: dict[str, Any] = {}
        if schema:
            data, parse_error = extract.parse_reply(text)
            errors = [{"path": "(reply)", "message": parse_error}] if parse_error else extract.validate(data, schema.schema)
            if parse_error is None:
                text = json.dumps(data, ensure_ascii=False, indent=2)  # readable in the text views, search & export
            filled, total = extract.count_fields(data, schema.schema)
            structured = {"schema": schema.schema, "schema_label": schema.label, "schema_preset": schema.preset,
                          "data": data, "data_errors": errors, "fields_filled": filled, "fields_total": total}
        record = {
            "provider": provider.id,
            "provider_label": provider.label,
            "model": result.model,
            "requested_model": model,
            "prompt_mode": spec.prompt_mode,
            "prompt": system_prompt if spec.prompt_mode == "custom" else None,
            "text": text,
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
            **structured,
        }
        doc.save_llm_result(page, key, record)
        return {**record, "key": key, "cached": False}

    async def draft_schema(self, provider_id: str, model: str | None, description: str,
                           current_schema: str | None = None, doc: OpenDocument | None = None,
                           page: int = 0) -> dict[str, Any]:
        """Ask a model to draft extraction fields from a plain-words description.

        With ``doc`` the model also sees that page (as an LLM would during
        extraction), which grounds the field names. With ``current_schema`` it
        changes that schema instead of starting over. Not cached: it's interactive
        and the user decides whether to keep the result. Respects ``llm_semaphore``.
        """
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError(f"unknown provider '{provider_id}'")
        if not provider.configured:
            raise ValueError(f"{provider.label} is not configured: set {', '.join(provider.missing_settings())}")
        if not description.strip():
            raise ValueError("describe what to extract, e.g. “invoice number, date, and each line item with its amount”")
        model = (model or provider.default_model or "").strip()
        image, mime = (await asyncio.to_thread(self.llm_image, doc, page)) if doc is not None else (b"", "")
        async with self.llm_semaphore:
            result = await provider.transcribe(TranscriptionRequest(
                image=image, mime_type=mime, system_prompt=DRAFT_SYSTEM,
                user_prompt=draft_user_prompt(description, current_schema, with_page=bool(image)),
                model=model, max_output_tokens=4000,
                output_schema=extract.provider_schema(extract.draft_schema()),
            ))
        data, error = extract.parse_reply(result.text)
        try:
            if error:
                raise extract.SchemaError(error)
            schema = extract.draft_to_schema(data)
        except extract.SchemaError as exc:
            raise ProviderError(f"{provider.label}: couldn't use the draft ({exc}). Try again, describe the fields "
                                "more concretely, or pick a model that enforces schemas") from exc
        return {
            "schema": schema,
            "notes": str(data.get("notes") or "").strip(),
            "provider": provider.id,
            "model": result.model,
            "used_page": bool(image),
            "latency_ms": result.latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost": self.cost(provider.id, result.model or model, result.input_tokens, result.output_tokens),
            "warnings": result.warnings,
        }

    # ------------------------------------------------------- batched LLM
    def plan_llm_chunks(self, doc: OpenDocument, pages: list[int], spec: LLMSpec,
                        force: bool = False) -> tuple[list[int], list[list[int]]]:
        """``(cached_pages, chunks)``: how a run over ``pages`` is split into requests.

        With one page per request every page is its own chunk and the cache is checked
        per page as before. With batching, pages already cached are left out first, so
        each request carries ``pages_per_request`` pages that really need the model.
        """
        n = spec.pages_per_request
        if n <= 1:
            return [], [[p] for p in pages]
        key = self.llm_key(doc, spec)
        cached = [] if force else [p for p in pages if doc.load_llm_result(p, key)]
        skip = set(cached)
        todo = [p for p in pages if p not in skip]
        return cached, [todo[i:i + n] for i in range(0, len(todo), n)]

    async def _send_pages(self, provider: Provider, model: str, system_prompt: str, pages: list[int],
                          images: dict[int, tuple[bytes, str]],
                          on_start: Callable[[], None] | None = None) -> TranscriptionResult:
        """One request carrying ``pages``, each labelled with its 1-based number."""
        first_image, first_mime = images[pages[0]]
        req = TranscriptionRequest(
            image=first_image, mime_type=first_mime, system_prompt=system_prompt, user_prompt=BATCH_USER_PROMPT,
            model=model, max_output_tokens=self.settings.llm_batch_max_output_tokens,
            pages=[PageImage(images[p][0], images[p][1], f"Page {p + 1}") for p in pages],
        )
        async with self.llm_semaphore:
            if on_start:
                on_start()
            try:
                return await provider.transcribe(req)
            except ProviderError as exc:
                if "token" in str(exc).lower():
                    raise ProviderError(f"{exc} (this request carried {len(pages)} page(s); if the model allows "
                                        f"fewer output tokens, lower LLM_BATCH_MAX_OUTPUT_TOKENS)") from exc
                raise

    async def run_llm_batch(self, doc: OpenDocument, pages: list[int], spec: LLMSpec, force: bool = False,
                            on_start: Callable[[], None] | None = None) -> dict[int, dict[str, Any] | Exception]:
        """Transcribe ``pages`` in one request and split the reply back into pages.

        Returns ``{page: result}`` for every page, where a result is the same dict
        :meth:`run_llm_page` returns, or the exception for that page. Cached pages are
        returned without a call. If the batched request itself fails, the error is
        raised: every page in it failed the same way.

        A page the reply can't be matched to (marker missing or repeated, or cut off by
        the output limit) is re-sent on its own, with the same prompt, so it still
        lands in this source; the record says so in ``warnings``. Tokens and latency of
        the shared request are split evenly over its pages, so per-page and per-source
        totals still add up to what was paid; ``batch`` holds the request's totals.
        """
        provider, model, system_prompt = self.resolve_spec(spec)
        pre = Preprocess.parse(spec.preprocess)
        key = doc.llm_cache_key(provider.id, model, system_prompt, str(pre))
        out: dict[int, dict[str, Any] | Exception] = {}
        todo: list[int] = []
        for p in pages:
            cached = None if force else doc.load_llm_result(p, key)
            if cached:
                out[p] = {**cached, "key": key, "cached": True}
            else:
                todo.append(p)
        if not todo:
            return out
        # the same images a one-page run would send (preprocessing included)
        images = {p: await asyncio.to_thread(self.llm_image, doc, p, pre) for p in todo}
        result = await self._send_pages(provider, model, system_prompt, todo, images, on_start)

        texts, problems = split_batch(result.text, [p + 1 for p in todo])
        truncated = result.stop_reason in _TRUNCATED
        shared_warnings = [w for w in result.warnings if "max token limit" not in w]
        if truncated and texts:
            # the last page in the reply was still being written when the limit hit
            texts.pop([p + 1 for p in todo if p + 1 in texts][-1])
            problems.append("the reply hit the output-token limit")
        size = len(todo)
        batch = {"pages": todo, "size": size, "input_tokens": result.input_tokens,
                 "output_tokens": result.output_tokens, "latency_ms": result.latency_ms, "problems": problems}

        def share(total: int | None, i: int) -> int | None:
            if total is None:
                return None
            q, r = divmod(total, size)
            return q + (1 if i < r else 0)

        def save(i: int, page: int, text: str, res: TranscriptionResult, warnings: list[str],
                 extra: TranscriptionResult | None = None) -> dict[str, Any]:
            def plus(a, b):
                return a if b is None else (a or 0) + b
            record = {
                "provider": provider.id,
                "provider_label": provider.label,
                "model": res.model,
                "requested_model": model,
                "prompt_mode": spec.prompt_mode,
                "prompt": system_prompt if spec.prompt_mode == "custom" else None,
                "text": text,
                "latency_ms": round(plus(result.latency_ms / size, extra.latency_ms if extra else None), 1),
                "input_tokens": plus(share(result.input_tokens, i), extra.input_tokens if extra else None),
                "output_tokens": plus(share(result.output_tokens, i), extra.output_tokens if extra else None),
                "stop_reason": res.stop_reason,
                "warnings": warnings,
                "image_bytes": len(images[page][0]),
                "image_mime": images[page][1],
                "preprocess": str(pre) or None,
                "preprocess_label": pre.describe() if not pre.is_identity else None,
                "created_at": time.time(),
                "pages_per_request": spec.pages_per_request,
                "batch": {**batch, "retried_alone": extra is not None},
            }
            doc.save_llm_result(page, key, record)
            return {**record, "key": key, "cached": False}

        async def alone(i: int, page: int) -> dict[str, Any] | Exception:
            reason = "; ".join(problems) or "the reply could not be matched to this page"
            try:
                single = await self._send_pages(provider, model, system_prompt, [page], images)
            except ProviderError as exc:
                return ProviderError(f"page {page + 1} was not in the batched reply ({reason}) and re-sending "
                                     f"it alone failed: {exc}")
            found, why = split_batch(single.text, [page + 1])
            if page + 1 not in found:
                return ProviderError(f"page {page + 1}: the model's reply could not be matched to this page "
                                     f"({'; '.join(why)}); try again or use one page per request")
            note = f"not in the batched reply ({reason}); transcribed on its own"
            return save(i, page, found[page + 1], single, [note, *single.warnings], extra=single)

        retry: list[tuple[int, int]] = []
        for i, page in enumerate(todo):
            if page + 1 in texts:
                out[page] = save(i, page, texts[page + 1], result, list(shared_warnings))
            else:
                retry.append((i, page))
        for (_, page), outcome in zip(retry, await asyncio.gather(*(alone(i, p) for i, p in retry))):
            out[page] = outcome
        return {p: out[p] for p in pages}

    async def run_llm_pages(self, doc: OpenDocument, pages: list[int], spec: LLMSpec,
                            force: bool = False) -> dict[int, dict[str, Any] | Exception]:
        """Transcribe many pages (one or several per request); ``{page: result or exception}``."""
        if spec.pages_per_request <= 1:
            results = await asyncio.gather(*(self.run_llm_page(doc, p, spec, force=force) for p in pages),
                                           return_exceptions=True)
            return dict(zip(pages, results))
        cached, chunks = self.plan_llm_chunks(doc, pages, spec, force)
        groups = ([cached] if cached else []) + chunks
        results = await asyncio.gather(*(self.run_llm_batch(doc, g, spec, force=force) for g in groups),
                                       return_exceptions=True)
        out: dict[int, dict[str, Any] | Exception] = {}
        for group, res in zip(groups, results):
            for p in group:
                out[p] = res if isinstance(res, BaseException) else res[p]
        return {p: out[p] for p in pages}

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

    # ------------------------------------------------------ PDF libraries
    def text_library(self, library_id: str | None) -> TextLibrary | None:
        """The library behind ``native:<id>``; ``None`` for PyMuPDF (the plain ``native`` source)."""
        if not library_id or library_id == DEFAULT_TEXT_LIBRARY:
            return None
        lib = self.text_libraries.get(library_id)
        if lib is None:
            known = ", ".join([DEFAULT_TEXT_LIBRARY, *self.text_libraries])
            raise DocumentError(f"unknown PDF library '{library_id}' (known: {known})")
        return lib

    def native_layer(self, doc: OpenDocument, page: int, library: str | None = None,
                     force: bool = False) -> TextLayer:
        """The page's embedded text as one PDF library reads it. Blocking: use a thread from async code."""
        lib = self.text_library(library)
        return doc.native_text(page) if lib is None else lib.extract(doc, page, force=force)

    def word_layer(self, doc: OpenDocument, page: int, source: str) -> TextLayer | None:
        """The text layer with word boxes behind a source, if it has one (native text and OCR do; LLMs don't).

        Only reads what is already there: OCR is not run, so asking never starts slow work.
        """
        if (library := native_library_of(source)) is not None:
            return self.native_layer(doc, page, library)
        if (engine := ocr_engine_of(source)) is not None:
            return doc.cached_ocr_text(page, engine)
        return None

    def compare_libraries(self, doc: OpenDocument, page: int) -> list[dict[str, Any]]:
        """Run every PDF library on one page: does each one get text out, and how much?"""
        rows = []
        for lib_id in [DEFAULT_TEXT_LIBRARY, *self.text_libraries]:
            lib = self.text_library(lib_id)
            row: dict[str, Any] = {"id": lib_id, "source_id": lib.source_id if lib else "native",
                                   "label": lib.label if lib else "PyMuPDF",
                                   "version": lib.version() if lib else pymupdf.VersionBind,
                                   "available": True, "problem": None, "error": None}
            if lib is not None and (problem := lib.problem()):
                rows.append({**row, "available": False, "problem": problem, "install": lib.install_hint()})
                continue
            try:
                layer = self.native_layer(doc, page, lib_id)
            except DocumentError as exc:
                rows.append({**row, "error": str(exc)})
                continue
            rows.append({**row, "chars": len(layer.text.strip()), "words": len(layer.text.split()),
                         "lines": sum(1 for ln in layer.text.splitlines() if ln.strip()),
                         "elapsed_ms": layer.elapsed_ms, "preview": layer.text.strip()[:200]})
        return rows

    # ----------------------------------------------------------------- sources
    # ------------------------------------------------------------ leaderboard
    #: at most this many sources vote in a page's consensus (as on the Compare page)
    MAX_VOTERS = 8

    @staticmethod
    def _independent(src: dict[str, Any]) -> bool:
        """Sources that read the page on their own, so they can vote in a consensus.

        Extra PDF libraries re-read the PyMuPDF text layer, and corrected text is
        the answer rather than a reading.
        """
        return src["kind"] in ("ocr", "llm") or src["id"] == "native"

    @staticmethod
    def _rankable(src: dict[str, Any]) -> bool:
        """Sources that are readings of the page: everything but corrected text and JSON extraction runs."""
        return src["kind"] != "corrected" and src.get("prompt_mode") != EXTRACT_MODE

    def leaderboard(self, docs: list[OpenDocument], reference: str = "auto", shared: bool = False,
                    opts: compare.NormalizeOptions | None = None) -> dict[str, Any]:
        """Rank every source across documents by how well it reads them.

        Each page is scored against the best reference it has: text a person
        corrected (``reference="corrected"``: real accuracy), else the consensus of
        three or more independent sources (``"consensus"``: agreement, not proof).
        ``"auto"`` takes corrected text where it exists and the consensus elsewhere.
        Pages with neither are skipped. With ``shared``, only pages that every
        ranked source has read count, so a source run on a few easy pages can't
        top the table. Rates are micro-averaged: total edits / total reference
        characters, so long pages weigh more.
        """
        if reference not in ("auto", "corrected", "consensus"):
            raise ValueError("reference must be auto, corrected or consensus")
        opts = opts or compare.NormalizeOptions(ignore_case=True)
        info: dict[str, dict[str, Any]] = {}
        pages: list[dict[str, Any]] = []  # one entry per page that has a reference
        skipped = 0
        for doc in docs:
            sources = self.list_sources(doc)
            ranked = [s for s in sources if self._rankable(s)]
            voters = [s["id"] for s in sources if self._independent(s) and self._rankable(s)][: self.MAX_VOTERS]
            for src in ranked:
                e = info.setdefault(src["id"], {"id": src["id"], "label": src["label"], "kind": src["kind"],
                                                "brand": src.get("brand", ""), "provider": src.get("provider"),
                                                "model": src.get("model"), "pages_run": 0, "input_tokens": 0,
                                                "output_tokens": 0, "latency_ms": 0.0, "has_usage": False})
                if src["kind"] == "llm":
                    e["pages_run"] += src["page_count"]
                    e["input_tokens"] += src.get("input_tokens") or 0
                    e["output_tokens"] += src.get("output_tokens") or 0
                    e["latency_ms"] += src.get("latency_ms") or 0
                    e["has_usage"] = True
            for n in doc.iter_pages():
                texts = {s["id"]: t for s in ranked if (t := self.page_text(doc, n, s["id"])) is not None}
                ref_text, ref_kind = None, None
                if reference in ("auto", "corrected"):
                    ref_text = self.page_text(doc, n, CORRECTED_SOURCE)
                    ref_kind = "corrected" if ref_text else None
                if ref_text is None and reference in ("auto", "consensus"):
                    votes = {sid: texts[sid] for sid in voters if sid in texts and texts[sid].strip()}
                    if len(votes) >= 3:
                        ref_text, ref_kind = compare.consensus(votes, opts)["text"], "consensus"
                if not ref_text or not texts:
                    skipped += 1
                    continue
                pages.append({"doc": doc.meta.id, "page": n, "reference": ref_kind, "texts": texts,
                              "ref": ref_text})
        ids = [i for i in info if any(i in p["texts"] for p in pages)]
        if shared:
            pages = [p for p in pages if all(i in p["texts"] for i in ids)]
        keys = ("char_distance", "reference_chars", "word_distance", "reference_words", "word_distance_order_free",
                "misread", "missed", "added", "numbers_total", "numbers_correct")
        tot = {i: dict.fromkeys(keys, 0) | {"pages": 0, "docs": set(), "by_doc": {}} for i in ids}
        for p in pages:
            for sid, text in p["texts"].items():
                if sid not in tot:
                    continue
                t, sc = tot[sid], compare.score(p["ref"], text, opts)
                br = compare.error_breakdown(p["ref"], text, opts, max_examples=0, max_confusions=0)
                num = compare.check_numbers(p["ref"], text, max_wrong=0)
                for k in ("char_distance", "reference_chars", "word_distance", "reference_words",
                          "word_distance_order_free"):
                    t[k] += sc[k]
                for k in ("misread", "missed", "added"):
                    t[k] += br[k]
                t["numbers_total"] += num["total"]
                t["numbers_correct"] += num["correct"]
                t["pages"] += 1
                t["docs"].add(p["doc"])
                d = t["by_doc"].setdefault(p["doc"], [0, 0])
                d[0] += sc["char_distance"]
                d[1] += sc["reference_chars"]

        def rate(num: int, den: int) -> float | None:
            return round(num / den, 4) if den else None

        entries = []
        for sid in ids:
            t, e = tot[sid], info[sid]
            if not t["pages"]:
                continue
            per_page = None
            if e["has_usage"] and e["pages_run"]:
                cost = self.cost(e["provider"] or "", e["model"], e["input_tokens"], e["output_tokens"])
                per_page = {"usd": cost["usd"] / e["pages_run"], "inr": cost["inr"] / e["pages_run"]} if cost else None
            entries.append({
                "id": sid, "label": e["label"], "kind": e["kind"], "brand": e["brand"],
                "pages": t["pages"], "docs": len(t["docs"]),
                "cer": rate(t["char_distance"], t["reference_chars"]),
                "wer": rate(t["word_distance"], t["reference_words"]),
                "wer_order_free": rate(t["word_distance_order_free"], t["reference_words"]),
                "errors": {k: t[k] for k in ("misread", "missed", "added")},
                "numbers": {"total": t["numbers_total"], "correct": t["numbers_correct"]},
                "by_doc": {d: rate(*v) for d, v in t["by_doc"].items()},
                "cost_per_page": per_page,
                "seconds_per_page": round(e["latency_ms"] / e["pages_run"] / 1000, 2)
                if e["has_usage"] and e["pages_run"] and e["latency_ms"] else None,
            })
        entries.sort(key=lambda r: (r["cer"] is None, r["cer"] or 0, -r["pages"]))
        by_ref = {"corrected": sum(p["reference"] == "corrected" for p in pages),
                  "consensus": sum(p["reference"] == "consensus" for p in pages)}
        return {"reference": reference, "shared": shared, "documents": len(docs),
                "docs": [{"id": d.meta.id, "filename": d.meta.filename} for d in docs
                         if any(p["doc"] == d.meta.id for p in pages)],
                "pages_scored": len(pages), "pages_by_reference": by_ref, "pages_skipped": skipped,
                "entries": entries}

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
        sources = [{"id": "native", "kind": "native", "library": DEFAULT_TEXT_LIBRARY,
                    "label": "Native text layer (PyMuPDF)" if doc.meta.is_pdf and any(
                        lib.available for lib in self.text_libraries.values()) else "Native text layer",
                    "pages": list(range(n))}]
        if doc.meta.is_pdf:
            # installed PDF libraries are read on demand (fast), so they cover every page
            sources += [{"id": lib.source_id, "kind": "native", "library": lib.id,
                         "label": f"Native text · {lib.label}", "pages": list(range(n))}
                        for lib in self.text_libraries.values() if lib.available]
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
                    "preprocess": r.get("preprocess"), "schema_label": r.get("schema_label"),
                    "pages_per_request": r.get("pages_per_request", 1),
                    "label": f"{r.get('provider_label') or r['provider']} · {r['model']} · {r.get('prompt_mode')}"
                             + (f" ({r['schema_label']})" if r.get("schema_label") else "")
                             + (f" · image: {r['preprocess_label']}" if r.get("preprocess_label") else "")
                             + (f" · {r['pages_per_request']} pages/request" if r.get("pages_per_request", 1) > 1
                                else ""),
                    "pages": [], "input_tokens": 0, "output_tokens": 0, "latency_ms": 0.0,
                    "updated_at": 0.0,
                })
                entry["pages"].append(p)
                entry["input_tokens"] += r.get("input_tokens") or 0
                entry["output_tokens"] += r.get("output_tokens") or 0
                entry["latency_ms"] += r.get("latency_ms") or 0
                entry["updated_at"] = max(entry["updated_at"], r.get("created_at") or 0)
        sources.extend(sorted(llm.values(), key=lambda e: e["updated_at"], reverse=True))
        if corrected := doc.corrected_pages():
            # listed first: it is ground truth, the natural reference for every other source
            sources.insert(0, {"id": CORRECTED_SOURCE, "kind": "corrected", "label": CORRECTED_LABEL,
                               "pages": corrected})
        for s in sources:
            s["page_count"] = len(s["pages"])
        return sources

    @staticmethod
    def _llm_result(doc: OpenDocument, page: int, source: str) -> dict[str, Any] | None:
        key = source[4:] if source.startswith("llm:") else None
        results = doc.list_llm_results(page)
        if key:
            results = [r for r in results if r["key"] == key]
        return results[0] if results else None

    def page_data(self, doc: OpenDocument, page: int, source: str) -> Any:
        """Parsed structured-extraction data of one page, or ``None`` (not an extraction, or not run)."""
        if source != "llm" and not source.startswith("llm:"):
            return None
        result = self._llm_result(doc, page, source)
        return result.get("data") if result else None

    def page_text(self, doc: OpenDocument, page: int, source: str, run_ocr: bool = False) -> str | None:
        """Text of one page from one source, or ``None`` if not extracted yet."""
        if source == CORRECTED_SOURCE:
            record = doc.load_corrected(page)
            return record["text"] if record else None
        if (library := native_library_of(source)) is not None:
            return self.native_layer(doc, page, library).text
        if (engine := ocr_engine_of(source)) is not None:
            layer = self.run_ocr_page(doc, page, engine=engine) if run_ocr else doc.cached_ocr_text(page, engine)
            return layer.text if layer else None
        if source == "llm" or source.startswith("llm:"):
            result = self._llm_result(doc, page, source)
            return result["text"] if result else None
        raise DocumentError(f"unknown source '{source}'")

    def document_texts(self, doc: OpenDocument) -> DocumentTexts:
        """Every source with at least one extracted page, and its text for every page (for "export everything").

        Uses the concrete ``llm:<key>`` sources, not the ``llm`` alias, so no run is hidden behind a newer one.
        Reads caches only: nothing is run.
        """
        pages = list(doc.iter_pages())
        return DocumentTexts(document=doc.meta.__dict__, sources=[
            SourceTexts(source=s, texts=[self.page_text(doc, n, s["id"]) for n in pages],
                        data=[self.page_data(doc, n, s["id"]) for n in pages] if s.get("schema_label") else None)
            for s in self.list_sources(doc) if s["page_count"]])

    def source_label(self, doc: OpenDocument, source: str) -> str:
        if source == CORRECTED_SOURCE:
            return CORRECTED_LABEL
        if (engine := ocr_engine_of(source)) is not None:
            return self.ocr_label(engine)
        if (library := native_library_of(source)) is not None and (lib := self.text_library(library)):
            return f"Native text · {lib.label}"
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


__all__ = ["CORRECTED_SOURCE", "ExtractSchema", "LLMSpec", "ProviderError", "Services", "ocr_engine_of", "ocr_source_id",
           "parse_pages"]
