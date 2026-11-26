"""Command line entry point.

    pyocr serve                      # start the web UI on http://127.0.0.1:8000
    pyocr providers                  # show which LLM providers are configured
    pyocr transcribe FILE -p anthropic --pages 1-3
    pyocr compare FILE -p openai --pages 1  # Tesseract/native vs LLM, printed as a table
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .config import get_settings


def _parse_pages(spec: str | None, page_count: int) -> list[int]:
    """'1-3,5' -> [0, 1, 2, 4] (input is 1-based, output 0-based)."""
    if not spec:
        return list(range(page_count))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part:
            pages.add(int(part))
    return sorted(p - 1 for p in pages if 1 <= p <= page_count)


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


def _cmd_providers(_args: argparse.Namespace) -> None:
    from .llm import build_providers

    for p in build_providers(get_settings()).values():
        status = "ready" if p.configured else "missing " + ", ".join(p.missing_settings())
        print(f"{p.id:20} {p.default_model or '-':28} {status}")


async def _transcribe_pages(path: Path, provider_id: str, model: str | None, pages_spec: str | None,
                            mode: str, compare_mode: bool) -> int:
    from . import compare
    from .documents import DocumentStore
    from .llm import USER_PROMPT, TranscriptionRequest, build_providers, resolve_prompt

    settings = get_settings()
    providers = build_providers(settings)
    provider = providers.get(provider_id)
    if provider is None:
        print(f"unknown provider {provider_id}; choose from {', '.join(providers)}", file=sys.stderr)
        return 2
    store = DocumentStore(settings.documents_dir)
    meta = store.add(path.name, path.read_bytes())
    doc = store.get(meta.id)
    system = resolve_prompt(mode)
    sem = asyncio.Semaphore(settings.llm_concurrency)

    async def one(n: int):
        image = await asyncio.to_thread(doc.render, n, settings.llm_dpi, "png", settings.llm_max_edge_px)
        async with sem:
            res = await provider.transcribe(TranscriptionRequest(image, "image/png", system, USER_PROMPT, model))
        key = doc.llm_cache_key(provider.id, model or provider.default_model or "", system)
        doc.save_llm_result(n, key, {"provider": provider.id, "model": res.model, "text": res.text,
                                     "prompt_mode": mode, "latency_ms": res.latency_ms,
                                     "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                                     "warnings": res.warnings, "created_at": time.time()})
        return n, res

    pages = _parse_pages(pages_spec, meta.page_count)
    results = await asyncio.gather(*(one(n) for n in pages))
    for n, res in sorted(results, key=lambda r: r[0]):
        if compare_mode:
            native = doc.native_text(n).text
            ocr = doc.ocr_text(n, dpi=settings.ocr_dpi, language=settings.tesseract_language).text
            opts = compare.NormalizeOptions(ignore_case=True)
            s_ocr = compare.score(res.text, ocr, opts)
            s_nat = compare.score(res.text, native, opts) if native.strip() else None
            nat = f"native CER {s_nat['cer']:.2%}" if s_nat else "no native text"
            print(f"page {n + 1:>4}: OCR CER {s_ocr['cer']:.2%}  WER {s_ocr['wer']:.2%} | {nat} "
                  f"| {res.latency_ms / 1000:.1f}s")
        else:
            print(f"\n===== Page {n + 1} ({res.provider}/{res.model}, {res.latency_ms / 1000:.1f}s) =====\n")
            print(res.text)
        for w in res.warnings:
            print(f"  warning: {w}", file=sys.stderr)
    print(f"\nstored as document {meta.id} - open it in the UI with `pyocr serve`", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pyocr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=_cmd_serve)

    p = sub.add_parser("providers", help="list LLM providers and whether they are configured")
    p.set_defaults(func=_cmd_providers)

    for name, is_compare in (("transcribe", False), ("compare", True)):
        t = sub.add_parser(name, help="transcribe pages with an LLM" if not is_compare
                           else "score Tesseract / native text against an LLM transcription")
        t.add_argument("file", type=Path)
        t.add_argument("-p", "--provider", default="anthropic")
        t.add_argument("-m", "--model", default=None)
        t.add_argument("--pages", default=None, help="1-based, e.g. 1-3,7 (default: all)")
        t.add_argument("--mode", default="verbatim", choices=["verbatim", "markdown"])
        t.set_defaults(func=lambda a, c=is_compare: sys.exit(asyncio.run(
            _transcribe_pages(a.file, a.provider, a.model, a.pages, a.mode, c))))

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
