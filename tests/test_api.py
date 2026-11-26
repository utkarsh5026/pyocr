from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pyocr.api import create_app
from pyocr.config import Settings
from pyocr.llm.base import Provider, TranscriptionRequest, TranscriptionResult

from .conftest import HAS_TESSERACT, TEXT_PAGE


class FakeProvider(Provider):
    id = "fake"
    label = "Fake"
    calls: list[TranscriptionRequest]

    def __init__(self):
        self.calls = []

    @property
    def default_model(self):
        return "fake-1"

    def missing_settings(self):
        return []

    async def _transcribe(self, req, model):
        self.calls.append(req)
        # pretend the LLM read the page perfectly except for one typo
        return TranscriptionResult(text=TEXT_PAGE.replace("lazy", "lazzy"), provider=self.id,
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
