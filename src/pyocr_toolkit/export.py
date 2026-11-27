"""Turn extracted text into downloadable files.

Two shapes of export exist:

* **one source** (``/export?source=...``): the whole document as read by one engine/model, for feeding a pipeline;
* **every source** (``/export/all``): everything extracted so far, side by side, so a user can take *all* their
  results away (or archive them) without picking a source first.

Both are pure formatting over texts that ``Services`` already read from the caches: an export never runs OCR or
an LLM. Page numbers are 0-based on the way in and 1-based in the files, because files are read by people and
by tools that number pages like the UI does.
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

from . import __version__

ONE_SOURCE_FORMATS = ("txt", "md", "json", "jsonl")
ALL_SOURCES_FORMATS = ("json", "jsonl", "csv", "md", "zip")
MEDIA_TYPES = {"txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
               "json": "application/json", "jsonl": "application/x-ndjson",
               "csv": "text/csv; charset=utf-8", "zip": "application/zip"}

# Source fields worth keeping in an export; the rest (page lists, timestamps) is UI bookkeeping.
_SOURCE_FIELDS = ("id", "kind", "label", "engine", "provider", "model", "prompt_mode", "preprocess",
                  "pages_per_request", "page_count", "input_tokens", "output_tokens")


@dataclass
class SourceTexts:
    """One source's text for every page (``None`` = not extracted for that page)."""

    source: dict[str, Any]
    texts: list[str | None]

    @property
    def id(self) -> str:
        return self.source["id"]

    @property
    def label(self) -> str:
        return self.source.get("label") or self.id


@dataclass
class DocumentTexts:
    document: dict[str, Any]
    sources: list[SourceTexts] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return int(self.document.get("page_count") or 0)


def file_tag(source: str) -> str:
    """Short, filename-safe tag for a source id (``ocr:easyocr`` → ``ocr-easyocr``, ``llm:<key>`` → ``llm``)."""
    return source.replace(":", "-") if source.startswith("ocr:") else source.split(":")[0]


# --------------------------------------------------------------- one source
def one_source(doc_meta: dict[str, Any], source: str, label: str, texts: list[str | None], fmt: str) -> str:
    if fmt == "json":
        return json.dumps({"document": doc_meta, "source": source, "source_label": label,
                           "pages": [{"page": i + 1, "text": t, "extracted": t is not None}
                                     for i, t in enumerate(texts)]}, ensure_ascii=False)
    if fmt == "jsonl":
        return "".join(json.dumps({"document_id": doc_meta["id"], "filename": doc_meta["filename"], "page": i + 1,
                                   "source": source, "text": t or ""}, ensure_ascii=False) + "\n"
                       for i, t in enumerate(texts))
    if fmt == "md":
        body = f"# {doc_meta['filename']}\n\n_Source: {label}_\n" + "".join(
            f"\n\n## Page {i + 1}\n\n{(t or '_(not extracted)_').strip()}" for i, t in enumerate(texts))
        return body + "\n"
    return "".join(f"\n\n===== Page {i + 1} =====\n\n{(t or '').strip()}" for i, t in enumerate(texts)).lstrip()


# -------------------------------------------------------------- all sources
def _source_info(s: SourceTexts) -> dict[str, Any]:
    return {k: s.source[k] for k in _SOURCE_FIELDS if s.source.get(k) is not None}


def all_json(data: DocumentTexts) -> str:
    """``{document, sources: [...], pages: [{page, texts: {source_id: text | null}}]}``."""
    return json.dumps({
        "pyocr_version": __version__,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "document": data.document,
        "sources": [_source_info(s) for s in data.sources],
        "pages": [{"page": n + 1, "texts": {s.id: s.texts[n] for s in data.sources}}
                  for n in range(data.page_count)],
    }, ensure_ascii=False, indent=2)


def all_jsonl(data: DocumentTexts) -> str:
    """One line per extracted (page, source) pair; pages a source never extracted are left out."""
    return "".join(
        json.dumps({"document_id": data.document["id"], "filename": data.document["filename"], "page": n + 1,
                    "source": s.id, "source_label": s.label, "text": t}, ensure_ascii=False) + "\n"
        for n in range(data.page_count) for s in data.sources if (t := s.texts[n]) is not None)


def all_csv(data: DocumentTexts) -> str:
    """A row per page and a column per source, so spreadsheets show the sources side by side.

    Starts with a UTF-8 BOM: without it Excel reads the file as the local code page and mangles non-ASCII text.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["page", *(f"{s.label} [{s.id}]" for s in data.sources)])
    for n in range(data.page_count):
        w.writerow([n + 1, *(s.texts[n] if s.texts[n] is not None else "" for s in data.sources)])
    return "﻿" + buf.getvalue()


def all_md(data: DocumentTexts) -> str:
    out = [f"# {data.document['filename']}\n\n_All extracted sources ({len(data.sources)})_"]
    for n in range(data.page_count):
        out.append(f"\n\n## Page {n + 1}")
        for s in data.sources:
            out.append(f"\n\n### {s.label}\n\n{(s.texts[n] or '_(not extracted)_').strip()}")
    return "".join(out) + "\n"


def all_zip(data: DocumentTexts, stem: str) -> bytes:
    """Everything in one archive: the combined JSON and CSV plus a TXT and Markdown file per source."""
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{stem}/all-sources.json", all_json(data))
        z.writestr(f"{stem}/all-sources.csv", all_csv(data))
        for i, s in enumerate(data.sources):
            name = file_tag(s.id)
            if name in used:  # several LLM runs all tag as "llm"
                name = f"{name}-{str(s.source.get('key') or i)[:8]}"
            used.add(name)
            for fmt in ("txt", "md"):
                z.writestr(f"{stem}/sources/{name}.{fmt}", one_source(data.document, s.id, s.label, s.texts, fmt))
    return buf.getvalue()
