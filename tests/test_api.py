from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pyocr_toolkit.api import create_app
from pyocr_toolkit.config import Settings
from pyocr_toolkit.llm.base import Provider, TranscriptionRequest, TranscriptionResult

from .conftest import HAS_TESSERACT, TEXT_PAGE


class FakeProvider(Provider):
    id = "fake"
    label = "Fake"
    calls: list[TranscriptionRequest]

    def __init__(self):
        self.calls = []
        self.typo = ("lazy", "lazzy")  # tests can change which word the fake misreads

    @property
    def default_model(self):
        return "fake-1"

    def missing_settings(self):
        return []

    async def _transcribe(self, req, model):
        self.calls.append(req)
        # pretend the LLM read the page perfectly except for one typo
        return TranscriptionResult(text=TEXT_PAGE.replace(*self.typo), provider=self.id,
                                   model=model, latency_ms=0, input_tokens=10, output_tokens=20)


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, _env_file=None))
    fake = FakeProvider()
    app.state.providers["fake"] = fake
    with TestClient(app) as c:
        c.fake = fake
        yield c


@pytest.fixture
def doc_id(client, sample_pdf):
    r = client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_index_and_config(client):
    assert "pyocr" in client.get("/").text
    cfg = client.get("/api/config").json()
    ids = {p["id"] for p in cfg["providers"]}
    assert {"anthropic", "openai", "azure_openai", "azure_claude", "gemini", "openai_compatible"} <= ids
    # no keys in the test environment -> everything except the fake is unconfigured
    assert all(not p["configured"] for p in cfg["providers"] if p["id"] != "fake")


def test_upload_precomputes_page_stats(client, tmp_path, sample_pdf, sample_png):
    """Uploads build pages.json in the background, so the first Overview visit doesn't wait for it."""
    one = client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf, "application/pdf")}).json()["id"]
    batch = client.post("/api/uploads", files=[("files", ("a.png", sample_png, "image/png"))]).json()
    client.app.state.store.wait_for_warmups()
    for d in (one, batch["documents"][0]["id"]):
        assert (tmp_path / "documents" / d / "pages.json").exists()


def test_upload_pages_image_text(client, doc_id):
    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    assert len(pages) == 2 and pages[1]["looks_scanned"]
    img = client.get(f"/api/documents/{doc_id}/pages/0/image?dpi=40")
    assert img.headers["content-type"] == "image/png"
    native = client.get(f"/api/documents/{doc_id}/pages/0/text/native").json()
    assert "quick brown fox" in native["text"] and native["words"]
    assert client.get(f"/api/documents/{doc_id}/pages/9/text/native").status_code == 400
    assert client.get("/api/documents/" + "0" * 32).status_code == 404


def test_bad_upload(client):
    r = client.post("/api/documents", files={"file": ("x.pdf", b"garbage", "application/pdf")})
    assert r.status_code == 400


def test_llm_run_cache_compare_search_export(client, doc_id):
    url = f"/api/documents/{doc_id}/pages/0/llm"
    r1 = client.post(url, json={"provider": "fake"}).json()
    assert r1["cached"] is False and "lazzy" in r1["text"]
    assert client.fake.calls[0].mime_type == "image/png"
    assert "Transcribe ALL visible text" in client.fake.calls[0].system_prompt
    r2 = client.post(url, json={"provider": "fake"}).json()
    assert r2["cached"] is True and len(client.fake.calls) == 1
    client.post(url, json={"provider": "fake", "force": True})
    assert len(client.fake.calls) == 2
    # a different prompt is a different cache entry
    client.post(url, json={"provider": "fake", "prompt_mode": "custom", "custom_prompt": "Just read it"})
    assert len(client.get(url).json()) == 2

    cmp = client.post(f"/api/documents/{doc_id}/pages/0/compare", json={
        "reference": {"source": "llm", "key": r1["key"]}, "hypothesis": {"source": "native"},
    }).json()
    assert 0 < cmp["metrics"]["cer"] < 0.05
    assert any(seg["op"] == "replace" and seg["ref"] == "lazzy" for seg in cmp["diff"])
    errs = cmp["errors"]
    assert errs["misread"] + errs["missed"] + errs["added"] == cmp["metrics"]["word_distance"]
    assert {"ref": "lazzy", "hyp": "lazy", "kind": "word"} in errs["examples"]
    assert cmp["numbers"]["total"] == cmp["numbers"]["correct"]
    assert [s["step"] for s in cmp["ladder"]][0] == "exact"

    doc_cmp = client.get(f"/api/documents/{doc_id}/compare", params={"a": f"llm:{r1['key']}", "b": "native"}).json()
    page0 = doc_cmp["pages"][0]
    assert page0["errors"] == {k: errs[k] for k in ("misread", "missed", "added")}
    assert doc_cmp["overall"]["errors"]["misread"] >= 1
    assert doc_cmp["overall"]["numbers"]["total"] >= page0["numbers"]["total"]

    res = client.get(f"/api/documents/{doc_id}/search", params={"q": "lazzy"}).json()
    assert res["total"] >= 1 and res["pages"][0]["hits"][0]["source"] == "llm"
    res = client.get(f"/api/documents/{doc_id}/search", params={"q": "brown fox", "sources": "native"}).json()
    assert res["pages"][0]["hits"][0]["boxes"]

    txt = client.get(f"/api/documents/{doc_id}/export", params={"source": "llm"}).text
    assert "===== Page 1 =====" in txt and "===== Page 2 =====" in txt
    rep = client.get(f"/api/documents/{doc_id}/report").json()
    assert rep["pages"][0]["native_vs_llm"]["cer"] > 0


def test_llm_errors(client, doc_id):
    url = f"/api/documents/{doc_id}/pages/0/llm"
    assert client.post(url, json={"provider": "nope"}).status_code == 400
    r = client.post(url, json={"provider": "anthropic"})
    assert r.status_code == 502 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert client.post(url, json={"provider": "fake", "prompt_mode": "custom"}).status_code == 400
    r = client.post(f"/api/documents/{doc_id}/pages/0/compare", json={})
    assert r.status_code == 400 and "no LLM transcription" in r.json()["detail"]


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_ocr_and_search_boxes(client, doc_id):
    ocr = client.get(f"/api/documents/{doc_id}/pages/1/text/ocr").json()
    assert "quick brown fox" in ocr["text"].lower()
    res = client.get(f"/api/documents/{doc_id}/search", params={"q": "receipt", "sources": "ocr"}).json()
    assert res["pages"][0]["page"] == 1 and res["pages"][0]["hits"][0]["boxes"]
    assert res["pages_without_ocr"] == 1


def test_consensus_outvotes_each_sources_own_mistake(client, doc_id):
    url = f"/api/documents/{doc_id}/pages/0/llm"
    k1 = client.post(url, json={"provider": "fake"}).json()["key"]  # reads "lazzy"
    client.fake.typo = ("quick", "quack")
    k2 = client.post(url, json={"provider": "fake", "prompt_mode": "custom", "custom_prompt": "Read it"}).json()["key"]
    ids = f"native,llm:{k1},llm:{k2}"

    res = client.get(f"/api/documents/{doc_id}/pages/0/consensus", params={"sources": ids}).json()
    # every source has one typo, and each typo is outvoted 2 to 1
    assert "lazy dog" in res["text"] and "quick brown" in res["text"]
    assert res["text"].splitlines()[0] == "Invoice 2024-117"  # line structure kept
    assert res["counts"]["tie"] == 0 and res["counts"]["majority"] == 2
    assert res["per_source"]["native"]["score"]["cer"] == 0
    assert res["per_source"][f"llm:{k1}"]["errors"] == {"misread": 1, "missed": 0, "added": 0}
    lazy = next(c for c in res["columns"] if c["winner"] == "lazy")
    assert lazy["votes"] == 2 and sorted(lazy["tokens"]) == ["lazy", "lazy", "lazzy"]
    assert res["matrix"]["native"]["native"] is None and res["matrix"]["native"][f"llm:{k1}"]["cer"] > 0

    doc = client.get(f"/api/documents/{doc_id}/consensus", params={"sources": ids}).json()
    assert doc["pages_scored"] == 1  # page 2 is a scan with no LLM run: no source has text there
    assert doc["pages"][1] == {"page": 1, "scored": False, "missing": ["native", f"llm:{k1}", f"llm:{k2}"]}
    assert doc["overall"]["native"]["cer"] == 0 and doc["overall"][f"llm:{k2}"]["cer"] > 0
    assert doc["matrix"][f"llm:{k1}"][f"llm:{k2}"] > 0


def test_consensus_errors(client, doc_id):
    url = f"/api/documents/{doc_id}/pages/0/consensus"
    assert client.get(url, params={"sources": "native"}).status_code == 400
    assert client.get(url, params={"sources": ",".join(f"llm:{i}" for i in range(9))}).status_code == 400
    r = client.get(url, params={"sources": "native,llm"})  # no LLM text yet: only one source votes
    assert r.status_code == 400 and "two or more" in r.json()["detail"]
    assert client.get(f"/api/documents/{doc_id}/pages/99/consensus", params={"sources": "native,llm"}).status_code == 400


def test_corrected_text_lifecycle(client, doc_id):
    url = f"/api/documents/{doc_id}/pages/0/corrected"
    assert client.get(url).status_code == 404
    assert all(src["id"] != "corrected" for src in client.get(f"/api/documents/{doc_id}/sources").json())

    fixed = "Invoice 2024-117\nThe quick brown fox jumps over the lazy dog.\nTotal due: $1,250.00"
    # writes are refused from other sites and must be JSON
    assert client.put(url, json={"text": fixed}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.put(url, content=fixed, headers={"Content-Type": "text/plain"}).status_code in (415, 422)
    assert client.put(url, json={"text": "   "}).status_code == 400
    assert client.put(f"/api/documents/{doc_id}/pages/9/corrected", json={"text": "x"}).status_code == 400

    saved = client.put(url, json={"text": fixed, "based_on": ["native", "ocr"]}).json()
    assert saved["text"] == fixed and saved["based_on"] == ["native", "ocr"] and saved["updated_at"] > 0
    assert client.get(url).json()["text"] == fixed

    # it is a source like any other: listed first, exported, and usable as a compare reference
    sources = client.get(f"/api/documents/{doc_id}/sources").json()
    assert sources[0]["id"] == "corrected" and sources[0]["pages"] == [0]
    export = client.get(f"/api/documents/{doc_id}/export", params={"source": "corrected"}).text
    assert "lazy dog" in export
    cmp = client.post(f"/api/documents/{doc_id}/pages/0/compare", json={
        "reference": {"source": "corrected"}, "hypothesis": {"source": "native"}}).json()
    assert cmp["reference"]["label"] == "Corrected (reviewed)" and cmp["metrics"]["cer"] == 0

    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404 and client.delete(url).status_code == 404


def test_consensus_page_has_word_boxes_and_review_state(client, doc_id):
    k = client.post(f"/api/documents/{doc_id}/pages/0/llm", json={"provider": "fake"}).json()["key"]
    url = f"/api/documents/{doc_id}/pages/0/consensus"
    res = client.get(url, params={"sources": f"native,llm:{k}"}).json()
    assert res["page_size"][0] > 0 and res["corrected"] is None
    # the native layer has word boxes, so every word gets one, the LLM's "lazzy" included
    lazy = next(c for c in res["columns"] if "lazzy" in c["tokens"])
    assert lazy["box"] and len(lazy["box"]) == 4 and not lazy["box_approx"]
    assert all(c["box"] for c in res["columns"])

    client.put(f"/api/documents/{doc_id}/pages/0/corrected", json={"text": res["text"], "based_on": res["sources"]})
    res = client.get(url, params={"sources": f"native,llm:{k}"}).json()
    assert res["corrected"]["based_on"] == ["native", f"llm:{k}"]


def test_leaderboard_ranks_sources_against_the_best_reference(client, doc_id):
    url = f"/api/documents/{doc_id}/pages/0/llm"
    k1 = client.post(url, json={"provider": "fake"}).json()["key"]  # reads "lazzy"
    client.fake.typo = ("quick brown", "quack")  # one misread word and one missed word
    k2 = client.post(url, json={"provider": "fake", "prompt_mode": "custom", "custom_prompt": "Read it"}).json()["key"]

    # no reviewed page yet: page 1 is scored against the consensus of native + two LLM runs
    lb = client.get("/api/leaderboard").json()
    assert lb["pages_by_reference"] == {"corrected": 0, "consensus": 1} and lb["pages_scored"] == 1
    assert lb["pages_skipped"] == 1  # page 2 is a scan: no corrected text and fewer than three voters
    by_id = {e["id"]: e for e in lb["entries"]}
    assert by_id["native"]["cer"] == 0 and by_id[f"llm:{k2}"]["cer"] > by_id[f"llm:{k1}"]["cer"] > 0
    assert by_id[f"llm:{k2}"]["errors"]["missed"] == 1
    assert [e["id"] for e in lb["entries"]].index(f"llm:{k1}") < [e["id"] for e in lb["entries"]].index(f"llm:{k2}")
    llm = by_id[f"llm:{k1}"]
    assert llm["pages"] == 1 and llm["docs"] == 1 and llm["seconds_per_page"] is None  # the fake reports 0 ms
    assert llm["by_doc"] == {doc_id: llm["cer"]}

    # a reviewed page is scored against the person's text instead
    client.put(f"/api/documents/{doc_id}/pages/0/corrected", json={"text": TEXT_PAGE.replace("lazy", "lazzy")})
    lb = client.get("/api/leaderboard", params={"reference": "corrected"}).json()
    assert lb["pages_by_reference"] == {"corrected": 1, "consensus": 0}
    by_id = {e["id"]: e for e in lb["entries"]}
    assert by_id[f"llm:{k1}"]["cer"] == 0 and by_id["native"]["cer"] > 0  # the "typo" was really on the page
    assert "corrected" not in by_id  # the answer is never ranked against itself
    assert client.get("/api/leaderboard", params={"reference": "consensus"}).json()["pages_by_reference"]["corrected"] == 0
    assert client.get("/api/leaderboard", params={"reference": "nope"}).status_code == 422


def test_leaderboard_scope_and_shared_pages(client, doc_id, sample_pdf):
    other = client.post("/api/documents", files={"file": ("other.pdf", sample_pdf, "application/pdf")}).json()["id"]
    for d in (doc_id, other):
        client.put(f"/api/documents/{d}/pages/0/corrected", json={"text": TEXT_PAGE})
    k = client.post(f"/api/documents/{doc_id}/pages/0/llm", json={"provider": "fake"}).json()["key"]

    lb = client.get("/api/leaderboard").json()
    by_id = {e["id"]: e for e in lb["entries"]}
    assert lb["pages_scored"] == 2 and by_id["native"]["pages"] == 2 and by_id[f"llm:{k}"]["pages"] == 1
    # only pages every source has read: the LLM ran on one document, so one page is left
    lb = client.get("/api/leaderboard", params={"shared": True}).json()
    assert lb["pages_scored"] == 1 and all(e["pages"] == 1 for e in lb["entries"])

    ws = client.post("/api/workspaces", json={"name": "Only the other one", "doc_ids": [other]}).json()
    lb = client.get("/api/leaderboard", params={"workspace": ws["id"]}).json()
    assert [d["id"] for d in lb["docs"]] == [other] and f"llm:{k}" not in {e["id"] for e in lb["entries"]}


def test_leaderboard_leaves_out_extraction_runs():
    from pyocr_toolkit.service import Services

    assert not Services._rankable({"kind": "llm", "prompt_mode": "extract"})
    assert not Services._rankable({"kind": "corrected"})
    assert Services._rankable({"kind": "llm", "prompt_mode": "verbatim"}) and Services._rankable({"kind": "native"})
    assert not Services._independent({"id": "native:pypdf", "kind": "native"})
