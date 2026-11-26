"""Workspaces: name suggestions, zip/batch ingestion, the API, the pages and `pyocr import`."""

from __future__ import annotations

import io
import json
import random
import zipfile

import pytest

from pyocr_toolkit import cli
from pyocr_toolkit.config import Settings
from pyocr_toolkit.documents import DocumentStore
from pyocr_toolkit.workspaces import (
    ADJECTIVES,
    SURNAMES,
    WorkspaceError,
    WorkspaceStore,
    ingest,
    slugify,
    suggest_names,
)

from .test_api import client, doc_id  # noqa: F401  (fixtures)


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def sample_zip(sample_pdf, sample_png) -> bytes:
    return _zip({
        "scans/invoice.pdf": sample_pdf,
        "scans/photo.png": sample_png,
        "notes.docx": b"not supported",
        "nested.zip": b"PK",
        "__MACOSX/scans/._invoice.pdf": b"junk",
        ".DS_Store": b"junk",
        "broken.pdf": b"garbage",
    })


# ---------------------------------------------------------------- names
def test_suggest_names_style_and_uniqueness():
    names = suggest_names(count=6, rng=random.Random(1))
    assert len(names) == len(set(names)) == 6
    for n in names:
        adj, surname = n.split("-")
        assert adj in ADJECTIVES and surname in SURNAMES
    # taken names are never suggested (case-insensitive)
    taken = [n.upper() for n in suggest_names(count=40, rng=random.Random(2))]
    assert not {n.lower() for n in suggest_names(taken=taken, count=10)} & {t.lower() for t in taken}


def test_suggest_names_from_files():
    assert slugify("Invoices 2024 (final).zip") == "invoices-2024-final"
    assert suggest_names(["Invoices 2024.zip"], count=1) == ["invoices-2024"]
    assert suggest_names(["invoice-jan.pdf", "invoice-feb.pdf"], count=1) == ["invoice"]
    assert suggest_names(["report-q1.pdf", "report-q2.pdf"], count=1) == ["report"]
    # nothing shared -> only random names
    assert suggest_names(["a.pdf", "zz.png"], count=1)[0].split("-")[0] in ADJECTIVES
    # a taken file-based name gets a suffix instead of disappearing
    assert suggest_names(["scans.zip"], taken=["scans"], count=1)[0].startswith("scans-")


# ---------------------------------------------------------------- store + ingest
def test_ingest_zip_skips_junk(tmp_path, sample_zip):
    store = DocumentStore(tmp_path / "documents")
    result = ingest(store, [("batch.zip", sample_zip)], max_bytes=50 * 1024 * 1024)
    assert sorted(m.filename for m in result.documents) == ["invoice.pdf", "photo.png"]
    reasons = {s["name"]: s["reason"] for s in result.skipped}
    assert "unsupported" in reasons["batch.zip/notes.docx"]
    assert "zip inside a zip" in reasons["batch.zip/nested.zip"]
    assert "could not open" in reasons["broken.pdf"]
    assert not any("MACOSX" in n or "DS_Store" in n for n in reasons)  # silently ignored
    with pytest.raises(WorkspaceError, match="not a valid zip"):
        ingest(store, [("bad.zip", b"nope")], max_bytes=1024)
    # files over the limit are skipped, not fatal
    assert ingest(store, [("big.pdf", b"x" * 20)], max_bytes=10).skipped[0]["reason"].startswith("larger than")


def test_workspace_store(tmp_path, sample_pdf):
    store = DocumentStore(tmp_path / "documents")
    spaces = WorkspaceStore(tmp_path / "workspaces", store)
    a, b = store.add("a.pdf", sample_pdf), store.add("b.pdf", sample_pdf)
    ws = spaces.create("Batch One", [a.id, b.id, a.id])
    assert ws.doc_ids == [a.id, b.id]
    assert spaces.create().name.count("-") == 1  # suggested name
    with pytest.raises(WorkspaceError, match="already exists"):
        spaces.create("batch one")
    with pytest.raises(WorkspaceError, match="empty"):
        spaces.rename(ws.id, "   ")
    assert spaces.rename(ws.id, "  Batch   Two ").name == "Batch Two"
    assert spaces.rename(ws.id, "batch two").name == "batch two"  # renaming to itself (other case) is fine
    # documents deleted behind its back disappear from the workspace
    store.delete(b.id)
    assert spaces.get(ws.id).doc_ids == [a.id]
    assert spaces.memberships()[a.id][0].id == ws.id
    spaces.delete(ws.id, delete_documents=True)
    assert store.list() == []
    with pytest.raises(WorkspaceError, match="unknown workspace"):
        spaces.get(ws.id)
    with pytest.raises(WorkspaceError, match="unknown workspace"):
        spaces.get("../../etc/passwd")


# ---------------------------------------------------------------- API
def test_api_batch_upload_into_workspace(client, sample_pdf, sample_png, sample_zip):  # noqa: F811
    names = client.post("/api/workspaces/suggest-names", json={"filenames": ["Q3 scans.zip"], "count": 3}).json()["names"]
    assert names[0] == "q3-scans" and len(names) == 3

    ws = client.post("/api/workspaces", json={"name": names[0]}).json()
    assert ws["document_count"] == 0
    assert client.post("/api/workspaces", json={"name": "Q3-SCANS"}).status_code == 400

    r = client.post("/api/uploads", data={"workspace_id": ws["id"]}, files=[
        ("files", ("one.pdf", sample_pdf, "application/pdf")),
        ("files", ("two.png", sample_png, "image/png")),
        ("files", ("batch.zip", sample_zip, "application/zip")),
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["documents"]) == 4 and len(body["skipped"]) == 3
    assert body["workspace"]["doc_ids"] == [d["id"] for d in body["documents"]]

    detail = client.get(f"/api/workspaces/{ws['id']}").json()
    assert detail["document_count"] == 4 and detail["page_count"] == 2 + 1 + 2 + 1
    assert {d["filename"] for d in detail["documents"]} == {"one.pdf", "two.png", "invoice.pdf", "photo.png"}

    renamed = client.patch(f"/api/workspaces/{ws['id']}", json={"name": "Quarter three"}).json()
    assert renamed["name"] == "Quarter three"
    assert client.get("/api/workspaces").json()[0]["name"] == "Quarter three"

    first = detail["documents"][0]["id"]
    left = client.delete(f"/api/workspaces/{ws['id']}/documents/{first}").json()
    assert first not in left["doc_ids"] and client.get(f"/api/documents/{first}").status_code == 200

    second = detail["documents"][1]["id"]
    assert client.delete(f"/api/documents/{second}").status_code == 204
    assert second not in client.get(f"/api/workspaces/{ws['id']}").json()["doc_ids"]

    # deleting the workspace keeps its documents unless asked
    assert client.delete(f"/api/workspaces/{ws['id']}").status_code == 204
    assert len(client.get("/api/documents").json()) == 3
    assert client.get(f"/api/workspaces/{ws['id']}").status_code == 404


def test_api_upload_errors(client, sample_pdf):  # noqa: F811
    r = client.post("/api/uploads", data={"workspace_id": "f" * 32}, files=[("files", ("a.pdf", sample_pdf))])
    assert r.status_code == 404 and "unknown workspace" in r.json()["detail"]
    assert client.get("/api/documents").json() == []  # nothing stored before the check
    r = client.post("/api/uploads", files=[("files", ("bad.zip", b"nope"))])
    assert r.status_code == 400 and "not a valid zip" in r.json()["detail"]
    # without a workspace the files just land in the library
    r = client.post("/api/uploads", files=[("files", ("a.pdf", sample_pdf)), ("files", ("x.txt.exe", b"?"))]).json()
    assert len(r["documents"]) == 1 and "workspace" not in r and "unsupported" in r["skipped"][0]["reason"]
    assert client.post("/api/workspaces", json={"name": "x" * 81}).status_code == 400
    assert client.post("/api/workspaces", json={"doc_ids": ["0" * 32]}).status_code == 404


def test_workspace_pages(client, doc_id):  # noqa: F811
    assert "No workspaces yet" in client.get("/workspaces").text
    ws = client.post("/api/workspaces", json={"name": "festive-albattani", "doc_ids": [doc_id]}).json()
    for url, needle in [
        ("/workspaces", "festive-albattani"),
        (f"/workspaces/{ws['id']}", "sample.pdf"),
        (f"/workspaces/{ws['id']}", f'data-ws-remove="{doc_id}"'),
        ("/documents", f'href="/workspaces/{ws["id"]}"'),  # badge on the library card
        ("/documents", 'id="lib-ws"'),
        (f"/documents/{doc_id}", "festive-albattani"),  # breadcrumb
        ("/", "festive-albattani"),
        ("/", 'id="upload-dialog"'),
    ]:
        r = client.get(url)
        assert r.status_code == 200 and needle in r.text, url
    r = client.get("/workspaces/" + "0" * 32)
    assert r.status_code == 404 and "unknown workspace" in r.text


# ---------------------------------------------------------------- CLI
def test_cli_import(tmp_path, monkeypatch, capsys, sample_pdf, sample_zip):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    folder = tmp_path / "in"
    (folder / ".hidden").mkdir(parents=True)
    (folder / "a.pdf").write_bytes(sample_pdf)
    (folder / ".hidden" / "b.pdf").write_bytes(sample_pdf)
    (folder / "readme.md").write_text("ignored")
    (tmp_path / "batch.zip").write_bytes(sample_zip)

    with pytest.raises(SystemExit) as exc:
        cli.main(["import", str(folder), str(tmp_path / "batch.zip"), "-w", "my batch"])
    assert exc.value.code == 0
    out = capsys.readouterr()
    assert "invoice.pdf" in out.out and "skipped" in out.out and "workspace 'my batch'" in out.err
    (ws_file,) = (tmp_path / "data" / "workspaces").glob("*.json")
    assert len(json.loads(ws_file.read_text())["doc_ids"]) == 3  # a.pdf + 2 from the zip

    with pytest.raises(SystemExit) as exc:
        cli.main(["import", str(folder / "a.pdf")])
    assert exc.value.code == 0 and "the library" in capsys.readouterr().err
    assert len(list((tmp_path / "data" / "workspaces").glob("*.json"))) == 1
