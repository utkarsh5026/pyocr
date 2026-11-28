"""Command line interface.

    pyocr serve [--open]             start the web app on http://127.0.0.1:8000
    pyocr init                       write a .env template to the current directory
    pyocr doctor                     check OCR engines, providers and the data directory
    pyocr providers                  list LLM providers and whether they are configured
    pyocr engines                    list OCR engines and whether they are ready
    pyocr libraries                  list PDF text libraries (pypdf, pdfplumber, ...) and whether they're installed
    pyocr extract FILE --engine llm -p anthropic --pages 1-3 --format jsonl -o out.jsonl
    pyocr extract FILE --engine ocr --ocr-engine easyocr      (or --engine ocr:easyocr)
    pyocr extract FILE --engine native:pypdf                  text layer as pypdf reads it
    pyocr compare FILE -p openai     per-page OCR/native error rate against an LLM
    pyocr import a.pdf b.pdf scans.zip [--workspace NAME]
                                     add files (zips unpacked, folders walked) to the library as one workspace
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import get_settings

ENV_TEMPLATE = Path(__file__).parent / "env.example"


# ---------------------------------------------------------------- helpers
def _services():
    from .llm import build_providers
    from .service import Services

    settings = get_settings()
    return settings, Services(settings, build_providers(settings))


def _write_output(text: str, out: Path | None) -> None:
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


# ---------------------------------------------------------------- commands
def cmd_serve(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .api import create_app

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"pyocr-toolkit {__version__} → {url}", file=sys.stderr)
    if args.open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    shutil.copyfile(ENV_TEMPLATE, target)
    print(f"wrote {target}: fill in the API keys you have, then run `pyocr serve`.")
    return 0


def cmd_providers(_args: argparse.Namespace) -> int:
    _, services = _services()
    for p in services.providers.values():
        status = "ready" if p.configured else "missing " + ", ".join(p.missing_settings())
        print(f"{p.id:20} {p.default_model or '-':28} {status}")
    return 0


def cmd_engines(_args: argparse.Namespace) -> int:
    _, services = _services()
    for e in services.ocr_engines.values():
        problem = e.problem()
        status = "ready" + (f" ({e.settings_summary()})" if e.settings_summary() else "") if not problem else problem
        print(f"{e.id:15} {e.kind:6} {status}")
    return 0


def cmd_libraries(_args: argparse.Namespace) -> int:
    import pymupdf

    _, services = _services()
    print(f"{'pymupdf':12} {'native':20} ready (PyMuPDF {pymupdf.VersionBind})")
    for lib in services.text_libraries.values():
        status = f"ready ({lib.label} {lib.version()})" if lib.available else lib.problem()
        print(f"{lib.id:12} {lib.source_id:20} {status}")
    return 0


def _ocr_engine_arg(args: argparse.Namespace) -> str:
    """``--engine ocr:easyocr`` and ``--engine ocr --ocr-engine easyocr`` mean the same."""
    engine = args.engine[4:] if args.engine.startswith("ocr:") else args.ocr_engine
    return engine or "tesseract"


def cmd_doctor(_args: argparse.Namespace) -> int:
    settings, services = _services()
    ok = True
    print(f"pyocr-toolkit {__version__} (Python {sys.version.split()[0]})")
    import pymupdf

    print(f"  PyMuPDF        {pymupdf.VersionBind}")
    tess = shutil.which("tesseract")
    if tess:
        out = subprocess.run([tess, "--list-langs"], capture_output=True, text=True).stdout
        langs = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]  # first line is a header
        want = settings.tesseract_language.split("+")
        missing = [lang for lang in want if lang not in langs]
        print(f"  Tesseract      {tess}  languages: {', '.join(langs) or '?'}")
        if missing:
            ok = False
            print(f"  ✗ language pack(s) missing for TESSERACT_LANGUAGE={settings.tesseract_language}: {', '.join(missing)}")
    else:
        print("  ✗ Tesseract not found (apt install tesseract-ocr / brew install tesseract)")
    engines = services.ocr_engines.values()
    print(f"  OCR engines    {sum(e.available for e in engines)} of {len(engines)} ready")
    for e in engines:
        problem = e.problem()
        print(f"     {'·' if problem else '✓'} {e.label} [{e.kind}]" + (f"  ({problem})" if problem else ""))
    libs = services.text_libraries.values()
    print(f"  PDF libraries  {sum(lib.available for lib in libs)} of {len(libs)} installed (optional; besides PyMuPDF)")
    for lib in libs:
        print(f"     {'✓' if lib.available else '·'} {lib.label}" + (f" {lib.version()}" if lib.available
                                                                   else f"  ({lib.install_hint()})"))
    data = settings.data_dir.resolve()
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        print(f"  Data dir       {data} (writable)")
    except OSError as exc:
        ok = False
        print(f"  ✗ Data dir     {data} is not writable: {exc}")
    from .remote import aws_status

    s3_ready, s3_detail = aws_status(settings)  # optional: a problem here is shown, not counted as a failure
    print(f"  S3 import      {'✓' if s3_ready else '·'} {s3_detail}")
    env = Path(".env")
    print(f"  .env         {'found' if env.exists() else 'not found (run `pyocr init`)'} in {Path.cwd()}")
    ready = [p for p in services.providers.values() if p.configured]
    print(f"  LLM providers  {len(ready)} of {len(services.providers)} configured")
    for p in services.providers.values():
        mark = "✓" if p.configured else "·"
        extra = "" if p.configured else f"  (set {', '.join(p.missing_settings())})"
        print(f"     {mark} {p.label}{extra}")
    if not ready:
        print("  Native text and OCR work without any provider; add a key to compare against LLMs.")
    return 0 if ok else 1


def cmd_import(args: argparse.Namespace) -> int:
    from itertools import chain

    from .documents import SUPPORTED_EXTENSIONS, DocumentError, DocumentStore
    from .remote import RemoteFetcher, is_remote, parse_locations
    from .workspaces import ARCHIVE_EXTENSIONS, IngestResult, WorkspaceStore, ingest, is_archive, suggest_names

    settings = get_settings()
    store = DocumentStore(settings.documents_dir)
    spaces = WorkspaceStore(settings.workspaces_dir, store)
    remote_args = [a for a in args.files if is_remote(a)]
    local_args = [Path(a) for a in args.files if not is_remote(a)]
    paths: list[Path] = []
    for path in local_args:
        if path.is_dir():
            wanted = SUPPORTED_EXTENSIONS | ARCHIVE_EXTENSIONS
            paths += sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in wanted
                            and not any(part.startswith(".") for part in p.relative_to(path).parts))
        elif path.is_file():
            paths.append(path)
        else:
            raise FileNotFoundError(f"no such file or folder: {path}")
    if not paths and not remote_args:
        raise ValueError("nothing to import: no supported files found")

    result = IngestResult()
    try:
        locations = parse_locations(remote_args) if remote_args else []
        # set up S3 / HTTP before touching the library, so a missing boto3 adds nothing
        remote_files = RemoteFetcher(settings, result, progress=lambda label: print(f"  ↓ {label}", file=sys.stderr)
                                     ).files(locations) if locations else iter(())
        names = [p.name for p in local_args] + [loc.name for loc in locations]
        grouped = not args.no_workspace and (args.workspace or len(paths) + len(locations) > 1
                                             or any(is_archive(p.name) for p in paths)
                                             or any(loc.is_folder for loc in locations))
        if grouped:
            name = args.workspace or suggest_names(names, taken=[w.name for w in spaces.list()], count=1)[0]
            ws = spaces.create(name)
        local_files = ((p.name, p.read_bytes()) for p in paths)
        result = ingest(store, chain(local_files, remote_files), max_bytes=settings.max_upload_mb * 1024 * 1024,
                        result=result)
        if grouped and result.documents:
            ws = spaces.add_documents(ws.id, [m.id for m in result.documents])
        elif grouped:
            spaces.delete(ws.id)  # nothing came in: don't leave an empty workspace behind
            grouped = False
    except DocumentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for meta in result.documents:
        print(f"  ✓ {meta.filename}  ({meta.page_count} page{'s' if meta.page_count != 1 else ''})  {meta.id}")
    for item in result.skipped:
        print(f"  · skipped {item['name']}: {item['reason']}")
    where = f"workspace '{ws.name}' ({ws.id})" if grouped else "the library"
    print(f"Added {len(result.documents)} document(s) to {where}.", file=sys.stderr)
    return 0 if result.documents else 1


def _load_document(path: Path):
    from .documents import DocumentStore

    settings = get_settings()
    store = DocumentStore(settings.documents_dir)
    meta = store.add(path.name, path.read_bytes())
    return store.get(meta.id)


async def _extract(args: argparse.Namespace) -> int:
    from .service import LLMSpec, parse_pages

    settings, services = _services()
    doc = _load_document(args.file)
    pages = parse_pages(args.pages, doc.meta.page_count)
    texts: dict[int, str] = {}
    data: dict[int, object] = {}  # structured extraction (--mode extract): parsed fields per page
    if args.engine == "native" or args.engine.startswith("native:"):
        library = args.engine[7:] or None if args.engine.startswith("native:") else None
        lib = services.text_library(library)
        if lib is not None and (problem := lib.problem()):
            print(problem, file=sys.stderr)
            return 2
        texts = {n: services.native_layer(doc, n, library).text for n in pages}
    elif args.engine == "ocr" or args.engine.startswith("ocr:"):
        engine = services.ocr_engine(_ocr_engine_arg(args))
        if problem := engine.problem():
            print(problem, file=sys.stderr)
            return 2
        for n in pages:
            layer = await asyncio.to_thread(services.run_ocr_page, doc, n, False, engine.id)
            texts[n] = layer.text
            print(f"  {engine.label} page {n + 1}/{doc.meta.page_count}: {(layer.elapsed_ms or 0) / 1000:.1f}s",
                  file=sys.stderr)
    else:
        schema = args.schema or ""
        if args.mode == "extract" and schema and not schema.lstrip().startswith("{") and Path(schema).is_file():
            schema = Path(schema).read_text(encoding="utf-8")
        elif args.mode != "extract" and schema:
            print("--schema only applies with --mode extract", file=sys.stderr)
            return 2
        spec = LLMSpec(args.provider, args.model, args.mode, preprocess=args.preprocess or "",
                       pages_per_request=args.pages_per_request, extract_schema=schema)
        try:
            provider, model, _ = services.resolve_spec(spec)
        except ValueError as exc:  # bad --pages-per-request, --preprocess or --schema
            print(str(exc), file=sys.stderr)
            return 2
        if not provider.configured:
            print(f"{provider.label} is not configured: set {', '.join(provider.missing_settings())}", file=sys.stderr)
            return 2

        for n, res in (await services.run_llm_pages(doc, pages, spec)).items():
            if isinstance(res, BaseException):
                print(f"  page {n + 1} failed: {res}", file=sys.stderr)
                continue
            note = "cached" if res["cached"] else f"{res['latency_ms'] / 1000:.1f}s"
            print(f"  LLM page {n + 1}: {note}" + "".join(f"  ⚠ {w}" for w in res["warnings"]), file=sys.stderr)
            for e in res.get("data_errors") or []:
                print(f"    ⚠ {e['path']}: {e['message']}", file=sys.stderr)
            texts[n] = res["text"]
            if "data" in res:
                data[n] = res["data"]

    ordered = [(n, texts.get(n, "")) for n in pages]
    from .service import ocr_source_id

    source = (f"llm:{args.provider}" if args.engine == "llm" else
              ocr_source_id(_ocr_engine_arg(args)) if args.engine.startswith("ocr") else args.engine)
    extra = (lambda n: {"data": data.get(n)}) if data else (lambda n: {})
    if args.format == "jsonl":
        out = "".join(json.dumps({"document_id": doc.meta.id, "filename": doc.meta.filename, "page": n + 1,
                                  "source": source, "text": t, **extra(n)}, ensure_ascii=False) + "\n"
                      for n, t in ordered)
    elif args.format == "json":
        out = json.dumps({"document": doc.meta.__dict__, "source": source,
                          "pages": [{"page": n + 1, "text": t, **extra(n)} for n, t in ordered]},
                         ensure_ascii=False, indent=1)
    elif args.format == "md":
        out = f"# {doc.meta.filename}\n" + "".join(f"\n## Page {n + 1}\n\n{t.strip()}\n" for n, t in ordered)
    else:
        out = "\n\n".join(f"===== Page {n + 1} =====\n\n{t.strip()}" for n, t in ordered)
    _write_output(out, args.output)
    print(f"document {doc.meta.id} is in the library; inspect it with `pyocr serve`", file=sys.stderr)
    return 0


async def _compare(args: argparse.Namespace) -> int:
    from . import compare
    from .service import LLMSpec, parse_pages

    settings, services = _services()
    doc = _load_document(args.file)
    spec = LLMSpec(args.provider, args.model, "verbatim", preprocess=args.preprocess or "")
    provider, _, _ = services.resolve_spec(spec)
    if not provider.configured:
        print(f"{provider.label} is not configured: set {', '.join(provider.missing_settings())}", file=sys.stderr)
        return 2
    pages = parse_pages(args.pages, doc.meta.page_count)
    llm = await asyncio.gather(*(services.run_llm_page(doc, n, spec) for n in pages))
    opts = compare.NormalizeOptions(ignore_case=True)
    engine = services.ocr_engine(args.ocr_engine)
    problem = engine.problem()
    if problem:
        print(f"OCR columns skipped: {problem}", file=sys.stderr)
    print(f"OCR engine: {engine.label}", file=sys.stderr)
    print(f"{'page':>5}  {'OCR CER':>8}  {'OCR WER':>8}  {'OCR numbers':>11}  {'native CER':>10}")
    for n, res in zip(pages, llm):
        native = doc.native_text(n).text
        ocr_cer = ocr_wer = ocr_num = "–"
        if not problem:
            layer = await asyncio.to_thread(services.run_ocr_page, doc, n, False, engine.id)
            ocr_text = layer.text
            s = compare.score(res["text"], ocr_text, opts)
            ocr_cer, ocr_wer = f"{s['cer']:.1%}", f"{s['wer']:.1%}"
            num = compare.check_numbers(res["text"], ocr_text, max_wrong=0)
            if num["total"]:
                ocr_num = f"{num['correct']}/{num['total']}"
        nat = f"{compare.score(res['text'], native, opts)['cer']:.1%}" if native.strip() else "no text"
        print(f"{n + 1:>5}  {ocr_cer:>8}  {ocr_wer:>8}  {ocr_num:>11}  {nat:>10}")
    print(f"\ndocument {doc.meta.id}: open http://127.0.0.1:8000/documents/{doc.meta.id}/compare for diffs",
          file=sys.stderr)
    return 0


# ---------------------------------------------------------------- parser
PREPROCESS_HELP = (
    "image preprocessing before the LLM sees the page: a preset (clean, contrast, bw, faded, denoise, detail, "
    "invert) or steps like 'gray;auto;contrast=1.5;threshold=otsu' (tune them visually with Adjust image in "
    "`pyocr serve`, then copy the spec)"
)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyocr", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"pyocr-toolkit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other machines (no auth!)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.add_argument("--log-level", default="info")
    s.set_defaults(func=cmd_serve)

    i = sub.add_parser("init", help="write a .env template here")
    i.add_argument("--path", default=".env")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="check the installation").set_defaults(func=cmd_doctor)
    sub.add_parser("providers", help="list LLM providers").set_defaults(func=cmd_providers)
    sub.add_parser("engines", help="list OCR engines").set_defaults(func=cmd_engines)
    sub.add_parser("libraries", help="list PDF text libraries (native:<library> sources)").set_defaults(
        func=cmd_libraries)

    e = sub.add_parser("extract", help="extract text from a PDF/image (whole document or pages)")
    e.add_argument("file", type=Path)
    e.add_argument("--engine", default="llm", metavar="{native,native:<library>,ocr,ocr:<engine>,llm}",
                   help="text source (default: llm); native:pypdf etc. read the text layer with another PDF "
                        "library, see `pyocr libraries`")
    e.add_argument("--ocr-engine", default="tesseract", help="for --engine ocr; see `pyocr engines`")
    e.add_argument("-p", "--provider", default="anthropic", help="for --engine llm")
    e.add_argument("-m", "--model", default=None)
    e.add_argument("--mode", default="verbatim", choices=["verbatim", "markdown", "extract"],
                   help="LLM prompt; 'extract' fills --schema with structured data (JSON) instead of transcribing")
    e.add_argument("--schema", default=None, metavar="SCHEMA",
                   help="for --mode extract: a preset (invoice, receipt, form), a JSON Schema file, or JSON text")
    e.add_argument("--pages-per-request", type=int, default=1, metavar="N",
                   help="send N pages to the LLM in one request (default 1); a separate source from N=1")
    e.add_argument("--pages", default=None, help="1-based ranges, e.g. 1-3,7 (default: all)")
    e.add_argument("--preprocess", default=None, metavar="SPEC", help=PREPROCESS_HELP)
    e.add_argument("--format", choices=["txt", "md", "json", "jsonl"], default="txt")
    e.add_argument("-o", "--output", type=Path, default=None, help="file to write (default: stdout)")
    e.set_defaults(func=lambda a: asyncio.run(_extract(a)))

    m = sub.add_parser("import", help="add files, folders, .zip archives, s3:// paths or https:// links as one workspace")
    m.add_argument("files", nargs="+", metavar="FILE|FOLDER|s3://bucket/key|s3://bucket/folder/|https://...")
    m.add_argument("-w", "--workspace", default=None, help="workspace name (default: a suggested one)")
    m.add_argument("--no-workspace", action="store_true", help="add to the library without grouping")
    m.set_defaults(func=cmd_import)

    c = sub.add_parser("compare", help="score OCR / native text against an LLM transcription")
    c.add_argument("file", type=Path)
    c.add_argument("--ocr-engine", default="tesseract", help="OCR engine to score; see `pyocr engines`")
    c.add_argument("-p", "--provider", default="anthropic")
    c.add_argument("-m", "--model", default=None)
    c.add_argument("--pages", default=None)
    c.add_argument("--preprocess", default=None, metavar="SPEC", help=PREPROCESS_HELP)
    c.set_defaults(func=lambda a: asyncio.run(_compare(a)))
    return parser


def main(argv: list[str] | None = None) -> None:
    # Windows consoles may not be UTF-8; never crash on a ✓ or an OCR'd glyph.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    from .documents import DocumentError
    from .llm import ProviderError

    parser = build_parser()
    args = parser.parse_args(argv)
    engine = getattr(args, "engine", None)
    if engine and engine not in ("native", "ocr", "llm") and not engine.startswith(("ocr:", "native:")):
        parser.error(f"--engine must be native, native:<library>, ocr, ocr:<engine> or llm (got '{engine}')")
    try:
        sys.exit(args.func(args))
    except (ValueError, FileNotFoundError, DocumentError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
