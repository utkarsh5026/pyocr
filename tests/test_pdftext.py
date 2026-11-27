"""Other PDF libraries (pypdf, pdfplumber, pdfminer.six, pypdfium2) as ``native:<library>`` sources.

Every library is optional, so each test that needs one skips when it isn't installed;
``pip install -e ".[dev]"`` installs them all.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from pyocr_toolkit import cli
from pyocr_toolkit.pdftext import TEXT_LIBRARY_CLASSES, native_library_of, native_source_id

from .conftest import TEXT_PAGE
from .test_api import client, doc_id  # noqa: F401  (fixtures)

LIBRARIES = [cls.id for cls in TEXT_LIBRARY_CLASSES]
MODULES = {cls.id: cls.module for cls in TEXT_LIBRARY_CLASSES}


def needs(lib: str):
    return pytest.mark.skipif(importlib.util.find_spec(MODULES[lib]) is None, reason=f"{lib} not installed")


def _words(text: str) -> list[str]:
    return text.split()


def test_source_ids():
    assert native_library_of("native") == "pymupdf"
    assert native_library_of("native:pypdf") == "pypdf"
    assert native_library_of("ocr") is None and native_library_of("llm:x") is None
    assert native_source_id("pymupdf") == "native" and native_source_id("pdfplumber") == "native:pdfplumber"


@pytest.mark.parametrize("lib", LIBRARIES)
def test_each_library_reads_the_text_layer(client, doc_id, lib):  # noqa: F811
    if importlib.util.find_spec(MODULES[lib]) is None:
        pytest.skip(f"{lib} not installed")
    r = client.get(f"/api/documents/{doc_id}/pages/0/text/native?library={lib}")
    assert r.status_code == 200, r.text
    layer = r.json()
    assert layer["source"] == f"native:{lib}" and layer["meta"]["library"] == lib and layer["meta"]["version"]
    assert _words(layer["text"]) == _words(TEXT_PAGE)
    # the scanned page has no text layer, for any library
    assert client.get(f"/api/documents/{doc_id}/pages/1/text/native?library={lib}").json()["text"].strip() == ""
    # cached on disk next to the other per-page caches
    assert client.app.state.store.get(doc_id).cached_native_layer(0, lib) is not None


@needs("pdfplumber")
def test_pdfplumber_word_boxes_match_pymupdf(client, doc_id):  # noqa: F811
    plumber = client.get(f"/api/documents/{doc_id}/pages/0/text/native?library=pdfplumber").json()["words"]
    mupdf = client.get(f"/api/documents/{doc_id}/pages/0/text/native").json()["words"]
    assert [w[4] for w in plumber] == [w[4] for w in mupdf]
    # same top-left origin in PDF points; fonts' ascent/descent differ a little between libraries
    for a, b in zip(plumber, mupdf):
        assert abs(a[0] - b[0]) < 2 and abs(a[1] - b[1]) < 6
    assert {w[6] for w in plumber} == {0, 1, 2}  # three lines


@needs("pypdf")
def test_sources_compare_and_export(client, doc_id):  # noqa: F811
    sources = {s["id"]: s for s in client.get(f"/api/documents/{doc_id}/sources").json()}
    assert sources["native:pypdf"]["kind"] == "native" and sources["native:pypdf"]["page_count"] == 2

    res = client.get(f"/api/documents/{doc_id}/compare", params={"a": "native", "b": "native:pypdf"}).json()
    assert res["b"]["label"] == "Native text · pypdf"
    assert res["pages"][0]["metrics"]["wer"] == 0

    res = client.post(f"/api/documents/{doc_id}/pages/0/compare", json={
        "reference": {"source": "native"}, "hypothesis": {"source": "native", "library": "pypdf"}}).json()
    assert res["metrics"]["cer"] == 0 and res["hypothesis"]["label"] == "Native text · pypdf"

    r = client.get(f"/api/documents/{doc_id}/export", params={"source": "native:pypdf", "fmt": "jsonl"})
    assert 'sample.native-pypdf.jsonl' in r.headers["content-disposition"]
    rows = [json.loads(line) for line in r.text.splitlines()]
    assert rows[0]["source"] == "native:pypdf" and "quick brown fox" in rows[0]["text"]


def test_libraries_report_and_config(client, doc_id, monkeypatch):  # noqa: F811
    lib = client.app.state.services.text_libraries["pypdf"]
    # pretend pypdf is missing: it's reported with the fix, never listed as a source
    monkeypatch.setattr(type(lib), "problem", lambda self: "pypdf is not installed: run pip install pypdf")
    rows = {r["id"]: r for r in client.get(f"/api/documents/{doc_id}/pages/0/text/native/libraries").json()}
    assert rows["pymupdf"]["chars"] == len(TEXT_PAGE.strip()) and rows["pymupdf"]["error"] is None
    assert rows["pypdf"]["available"] is False and "pip install pypdf" in rows["pypdf"]["install"]
    assert set(rows) == {"pymupdf", *LIBRARIES}
    assert "native:pypdf" not in {s["id"] for s in client.get(f"/api/documents/{doc_id}/sources").json()}
    r = client.get(f"/api/documents/{doc_id}/pages/0/text/native?library=pypdf")
    assert r.status_code == 503 and "pip install" in r.json()["detail"]

    libs = {lib["id"]: lib for lib in client.get("/api/config").json()["text_libraries"]}
    assert libs["pymupdf"]["available"] and libs["pypdf"]["available"] is False


def test_unknown_library_and_non_pdf(client, sample_png):  # noqa: F811
    r = client.post("/api/documents", files={"file": ("img.png", sample_png, "image/png")})
    png_id = r.json()["id"]
    assert client.get(f"/api/documents/{png_id}/pages/0/text/native?library=nope").status_code in (400, 404)
    sources = [s["id"] for s in client.get(f"/api/documents/{png_id}/sources").json()]
    assert not any(s.startswith("native:") for s in sources)  # images have no PDF text layer
    rows = client.get(f"/api/documents/{png_id}/pages/0/text/native/libraries").json()
    installed = [r for r in rows if r["id"] != "pymupdf" and r["available"]]
    assert all("PDF files only" in r["error"] for r in installed)


@needs("pypdf")
def test_cache_is_redone_when_the_library_changes(client, doc_id, monkeypatch):  # noqa: F811
    services = client.app.state.services
    doc = client.app.state.store.get(doc_id)
    lib = services.text_libraries["pypdf"]
    first = services.native_layer(doc, 0, "pypdf")
    stale = first.to_json() | {"text": "stale", "meta": {**first.meta, "version": "0.0.1"}}
    (doc.folder / "native" / "pypdf" / "0.json").write_text(json.dumps(stale))
    assert services.native_layer(doc, 0, "pypdf").text == first.text  # other version -> re-extracted
    monkeypatch.setattr(type(lib), "version", lambda self: "0.0.1")
    (doc.folder / "native" / "pypdf" / "0.json").write_text(json.dumps(stale))
    assert services.native_layer(doc, 0, "pypdf").text == "stale"  # same version -> cache hit


@needs("pypdf")
def test_cli_extract_with_library(tmp_path, monkeypatch, sample_pdf, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    cli.get_settings.cache_clear()
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(sample_pdf)
    with pytest.raises(SystemExit) as e:
        cli.main(["extract", str(pdf), "--engine", "native:pypdf", "--pages", "1", "--format", "jsonl"])
    assert e.value.code == 0
    row = json.loads(capsys.readouterr().out.splitlines()[0])
    assert row["source"] == "native:pypdf" and "quick brown fox" in row["text"]
    with pytest.raises(SystemExit) as e:
        cli.main(["libraries"])
    assert e.value.code == 0 and "native:pdfplumber" in capsys.readouterr().out
    cli.get_settings.cache_clear()
