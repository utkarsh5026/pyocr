"""Workspaces: named groups of documents, plus batch and zip ingestion.

People rarely check one file. They get a folder of invoices or a zip of
scans. A workspace keeps such a batch together under one name the user can
change, so it can be found and handled as a set.

Workspaces don't change the document layout. A document stays in
``data/documents/<id>/`` and workspaces only *reference* document ids::

    data/workspaces/<workspace_id>.json   # {"id", "name", "doc_ids", ...}

So existing data folders keep working, a workspace can be deleted without
losing its documents, and a document can be in more than one workspace.
Ids that point at deleted documents are dropped when a workspace is read.

Name suggestions follow the ``adjective-scientist`` pattern (``festive-albattani``),
the way Claude Code and Docker name things: short, readable and unlikely to
clash. When the uploaded files share a name (``invoices-2024.zip``), that name
comes first.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import uuid
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath

from .documents import SUPPORTED_EXTENSIONS, DocumentError, DocumentMeta, DocumentStore

_WS_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_NAME_LEN = 80
#: Zip safety limits: number of members, and total unpacked size as a multiple of MAX_UPLOAD_MB.
MAX_ARCHIVE_MEMBERS = 500
ARCHIVE_TOTAL_FACTOR = 5
ARCHIVE_EXTENSIONS = {".zip"}  # .cbz is a zip too, but PyMuPDF opens it as a comic book document

ADJECTIVES = [
    "agile", "amber", "bold", "brave", "bright", "calm", "clever", "cosmic", "crisp", "curious", "daring",
    "eager", "elegant", "festive", "fluent", "gentle", "gleaming", "golden", "happy", "humble", "intelligent",
    "jolly", "keen", "kind", "lively", "lucid", "merry", "modest", "nimble", "noble", "patient", "peaceful",
    "quiet", "quick", "radiant", "serene", "sharp", "steady", "sunny", "swift", "tidy", "tranquil", "upbeat",
    "vibrant", "vigilant", "vivid", "warm", "wise", "witty", "zealous",
]
# Scientists, mathematicians and printing / computing pioneers.
SURNAMES = [
    "albattani", "babbage", "bohr", "boole", "cannon", "curie", "darwin", "dijkstra", "einstein", "euclid",
    "faraday", "fermi", "franklin", "galileo", "gauss", "goodall", "gutenberg", "hamilton", "hopper", "hypatia",
    "johnson", "kepler", "knuth", "lamarr", "lovelace", "maxwell", "meitner", "mirzakhani", "newton", "noether",
    "pascal", "pasteur", "planck", "ramanujan", "ride", "sagan", "shannon", "tesla", "turing", "wozniak",
]


class WorkspaceError(DocumentError):
    """User-facing workspace problem (unknown id, bad name, bad archive)."""


@dataclass
class Workspace:
    id: str
    name: str
    created_at: float
    updated_at: float
    doc_ids: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    """What a batch upload produced: new documents and files that were skipped (with why)."""

    documents: list[DocumentMeta] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"documents": [m.__dict__ for m in self.documents], "skipped": self.skipped}


# ---------------------------------------------------------------- names
def slugify(text: str) -> str:
    """``"Invoices 2024 (final).zip"`` -> ``"invoices-2024-final"``."""
    stem = PurePosixPath(text).stem if "." in text else text
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:MAX_NAME_LEN]


def random_name(rng: random.Random | None = None) -> str:
    rng = rng or random
    return f"{rng.choice(ADJECTIVES)}-{rng.choice(SURNAMES)}"


def _name_from_files(filenames: list[str]) -> str | None:
    """A name the files themselves suggest: a single file's stem, or a common prefix."""
    stems = [slugify(PurePosixPath(f).name) for f in filenames if f]
    stems = [s for s in stems if s]
    if not stems:
        return None
    if len(stems) == 1:
        return stems[0]
    prefix = os.path.commonprefix(stems).strip("-_ ")
    # cut back to a whole word so "invoice-jan", "invoice-feb" gives "invoice", not "invoice-"
    if prefix and not all(len(s) == len(prefix) or s[len(prefix)] == "-" for s in stems):
        prefix = prefix.rsplit("-", 1)[0] if "-" in prefix else prefix
    return prefix if len(prefix) >= 3 else None


def suggest_names(filenames: Iterable[str] = (), taken: Iterable[str] = (), count: int = 4,
                  rng: random.Random | None = None) -> list[str]:
    """Up to ``count`` distinct workspace names not in ``taken`` (case-insensitive)."""
    rng = rng or random.Random()
    used = {t.lower() for t in taken}
    out: list[str] = []

    def add(name: str) -> None:
        if name and name.lower() not in used:
            used.add(name.lower())
            out.append(name)

    base = _name_from_files(list(filenames))
    if base:
        if base.lower() in used:
            base = f"{base}-{rng.choice(SURNAMES)}"
        add(base)
    for _ in range(200):
        if len(out) >= count:
            break
        add(random_name(rng))
    return out[:count]


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise WorkspaceError("workspace name can't be empty")
    if len(name) > MAX_NAME_LEN:
        raise WorkspaceError(f"workspace name is too long (max {MAX_NAME_LEN} characters)")
    return name


# ---------------------------------------------------------------- store
class WorkspaceStore:
    """One JSON file per workspace under ``root``. Writes are atomic and serialised by a lock."""

    def __init__(self, root: Path, documents: DocumentStore) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.documents = documents
        self._lock = threading.RLock()

    def _path(self, ws_id: str) -> Path:
        if not _WS_ID_RE.match(ws_id or ""):
            raise WorkspaceError("unknown workspace")
        return self.root / f"{ws_id}.json"

    def _write(self, ws: Workspace) -> None:
        path = self._path(ws.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(ws), ensure_ascii=False, indent=1))
        tmp.replace(path)

    def _read(self, path: Path) -> Workspace:
        ws = Workspace(**json.loads(path.read_text()))
        # drop documents that were deleted outside a workspace
        ws.doc_ids = [d for d in ws.doc_ids if (self.documents.root / d / "meta.json").exists()]
        return ws

    def check_new_name(self, name: str) -> str:
        """The cleaned name, or WorkspaceError if it's empty, too long or taken. For checks before slow work."""
        name = clean_name(name)
        self._check_unique(name)
        return name

    def _check_unique(self, name: str, except_id: str | None = None) -> None:
        for ws in self.list():
            if ws.id != except_id and ws.name.lower() == name.lower():
                raise WorkspaceError(f"a workspace called '{name}' already exists; pick another name")

    def list(self) -> list[Workspace]:
        out = []
        for path in self.root.glob("*.json"):
            try:
                out.append(self._read(path))
            except (ValueError, TypeError):
                continue
        return sorted(out, key=lambda w: w.created_at, reverse=True)

    def get(self, ws_id: str) -> Workspace:
        path = self._path(ws_id)
        if not path.exists():
            raise WorkspaceError("unknown workspace")
        return self._read(path)

    def create(self, name: str | None = None, doc_ids: Iterable[str] = ()) -> Workspace:
        with self._lock:
            if name is None or not name.strip():
                name = suggest_names(taken=[w.name for w in self.list()], count=1)[0]
            name = clean_name(name)
            self._check_unique(name)
            now = time.time()
            ws = Workspace(uuid.uuid4().hex, name, now, now)
            ws.doc_ids = self._valid_ids(doc_ids)
            self._write(ws)
            return ws

    def _valid_ids(self, doc_ids: Iterable[str]) -> list[str]:
        ids = []
        for d in doc_ids:
            self.documents.get(d)  # raises "unknown document"
            if d not in ids:
                ids.append(d)
        return ids

    def rename(self, ws_id: str, name: str) -> Workspace:
        with self._lock:
            ws = self.get(ws_id)
            name = clean_name(name)
            self._check_unique(name, except_id=ws_id)
            ws.name, ws.updated_at = name, time.time()
            self._write(ws)
            return ws

    def add_documents(self, ws_id: str, doc_ids: Iterable[str]) -> Workspace:
        with self._lock:
            ws = self.get(ws_id)
            for d in self._valid_ids(doc_ids):
                if d not in ws.doc_ids:
                    ws.doc_ids.append(d)
            ws.updated_at = time.time()
            self._write(ws)
            return ws

    def remove_document(self, ws_id: str, doc_id: str) -> Workspace:
        with self._lock:
            ws = self.get(ws_id)
            ws.doc_ids = [d for d in ws.doc_ids if d != doc_id]
            ws.updated_at = time.time()
            self._write(ws)
            return ws

    def forget_document(self, doc_id: str) -> None:
        """Called when a document is deleted: remove it from every workspace."""
        with self._lock:
            for ws in self.list():
                if doc_id in ws.doc_ids:
                    self.remove_document(ws.id, doc_id)

    def delete(self, ws_id: str, *, delete_documents: bool = False) -> None:
        with self._lock:
            ws = self.get(ws_id)
            self._path(ws_id).unlink()
            if delete_documents:
                for d in ws.doc_ids:
                    self.documents.delete(d)
                    self.forget_document(d)

    def memberships(self) -> dict[str, list[Workspace]]:
        """doc_id -> workspaces that contain it (for badges and breadcrumbs)."""
        out: dict[str, list[Workspace]] = {}
        for ws in self.list():
            for d in ws.doc_ids:
                out.setdefault(d, []).append(ws)
        return out


# ---------------------------------------------------------------- ingestion
def is_archive(filename: str) -> bool:
    return PurePosixPath(filename).suffix.lower() in ARCHIVE_EXTENSIONS


def _archive_members(filename: str, data: bytes, max_bytes: int, result: IngestResult):
    """Yield ``(name, bytes)`` for each supported file in a zip, recording the rest in ``result.skipped``."""
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise WorkspaceError(f"'{filename}' is not a valid zip file ({exc})") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise WorkspaceError(f"'{filename}' has {len(infos)} files; the limit is {MAX_ARCHIVE_MEMBERS} per zip. "
                                 "Split it into smaller zips.")
        total = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            label = f"{filename}/{info.filename}"
            # macOS resource forks, dotfiles and anything inside hidden folders are noise, not documents
            if any(part.startswith(".") or part == "__MACOSX" for part in path.parts):
                continue
            ext = path.suffix.lower()
            if ext in ARCHIVE_EXTENSIONS:
                result.skipped.append({"name": label, "reason": "zip inside a zip; unzip it first"})
                continue
            if ext not in SUPPORTED_EXTENSIONS:
                result.skipped.append({"name": label, "reason": f"unsupported file type '{ext or '(none)'}'"})
                continue
            if info.flag_bits & 0x1:
                result.skipped.append({"name": label, "reason": "encrypted zip entry"})
                continue
            if info.file_size > max_bytes:
                result.skipped.append({"name": label, "reason": f"larger than {max_bytes // (1024 * 1024)} MB"})
                continue
            total += info.file_size
            if total > max_bytes * ARCHIVE_TOTAL_FACTOR:
                raise WorkspaceError(f"'{filename}' unpacks to more than {ARCHIVE_TOTAL_FACTOR * max_bytes // (1024 * 1024)} MB. "
                                     "Split it or raise MAX_UPLOAD_MB.")
            yield path.name, zf.read(info)


def ingest(store: DocumentStore, files: Iterable[tuple[str, bytes]], *, max_bytes: int,
           result: IngestResult | None = None) -> IngestResult:
    """Add every file to ``store``, expanding zip archives. Bad files are skipped, not fatal.

    Only a broken archive or one over the safety limits raises, because then
    nothing in it can be trusted. Pass ``result`` when ``files`` is a generator
    that records its own skips there (``remote.py``), so both kinds stay in order.
    """
    result = result if result is not None else IngestResult()

    def add_one(name: str, data: bytes) -> None:
        try:
            result.documents.append(store.add(name, data))
        except DocumentError as exc:
            result.skipped.append({"name": name, "reason": str(exc)})

    for filename, data in files:
        if len(data) > max_bytes:
            result.skipped.append({"name": filename, "reason": f"larger than {max_bytes // (1024 * 1024)} MB"})
        elif is_archive(filename):
            for name, member in _archive_members(filename, data, max_bytes, result):
                add_one(name, member)
        else:
            add_one(filename, data)
    return result
