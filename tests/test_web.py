"""HTML pages, background jobs, document-level compare, export and CLI."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pyocr_toolkit import cli
from pyocr_toolkit.api import create_app
from pyocr_toolkit.config import Settings
from pyocr_toolkit.documents import DocumentStore
from pyocr_toolkit.jobs import Job, JobManager
from pyocr_toolkit.service import LLMSpec, Services, parse_pages

from .conftest import HAS_TESSERACT
from .test_api import FakeProvider


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
    return client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf)}).json()["id"]


def wait_for_job(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_parse_pages():
    assert parse_pages(None, 3) == [0, 1, 2]
    assert parse_pages("all", 3) == [0, 1, 2]
    assert parse_pages("1-2, 5", 10) == [0, 1, 4]
    assert parse_pages("2-99", 3) == [1, 2]
    with pytest.raises(ValueError):
        parse_pages("x", 3)
    with pytest.raises(ValueError):
        parse_pages("7", 3)


def test_html_pages_render(client, doc_id):
    for url, needle in [
        ("/", "See exactly what your text extraction produces"),
        ("/documents", "sample.pdf"),
        (f"/documents/{doc_id}", "Extract text"),
        (f"/documents/{doc_id}/pages/1", 'data-page="0"'),
        (f"/documents/{doc_id}/pages/99", 'data-page="1"'),  # clamped to the last page
        (f"/documents/{doc_id}/text", "source-select"),
        (f"/documents/{doc_id}/compare", "Compare two sources"),
        ("/search?q=fox", "Search all documents"),
        ("/jobs", "Extraction jobs"),
        ("/providers", "ANTHROPIC_API_KEY"),
        ("/models", "Models your key can use"),
        ("/help", "first principles"),
        ("/workspaces", "Workspaces"),
    ]:
        r = client.get(url)
        assert r.status_code == 200, url
        assert needle in r.text, url
    assert client.get(f"/documents/{doc_id}/pages", follow_redirects=False).status_code == 307
    r = client.get("/documents/" + "0" * 32)
    assert r.status_code == 404 and "unknown document" in r.text and "<html" in r.text
    # static assets referenced by the templates exist
    for asset in ["common.js", "workspace.js", "document.js", "reader.js", "compare.js", "search.js",
                  "jobs.js", "providers.js", "models.js", "library.js", "workspaces.js", "styles.css",
                  "logos/claude.svg"]:
        assert client.get(f"/static/{asset}").status_code == 200, asset


def test_providers_page_never_shows_values(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, _env_file=None, openai_api_key="sk-secret-123"))
    with TestClient(app) as c:
        html = c.get("/providers").text
        cfg = c.get("/api/config").text
    assert "sk-secret-123" not in html and "sk-secret-123" not in cfg
    assert "</svg>set</span>" in html  # check icon + "set"
    # built-in defaults are not reported as "set" by the user
    assert "default: gemini-3.8-flash" in html


def test_llm_job_sources_compare_export(client, doc_id):
    body = {"kind": "llm", "llm": {"provider": "fake"}}
    job = client.post(f"/api/documents/{doc_id}/jobs", json=body).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done" and job["done"] == 2 and job["failed"] == 0
    assert job["input_tokens"] == 20
    # a second run is served from cache
    job2 = wait_for_job(client, client.post(f"/api/documents/{doc_id}/jobs", json=body).json()["id"])
    assert job2["cached"] == 2 and len(client.fake.calls) == 2
    assert {j["id"] for j in client.get(f"/api/jobs?doc_id={doc_id}").json()} == {job["id"], job2["id"]}

    sources = client.get(f"/api/documents/{doc_id}/sources").json()
    llm = [s for s in sources if s["kind"] == "llm"]
    assert len(llm) == 1 and llm[0]["page_count"] == 2
    src = llm[0]["id"]

    text = client.get(f"/api/documents/{doc_id}/text", params={"source": src}).json()
    assert all("lazzy" in p["text"] for p in text["pages"])
    ocr_text = client.get(f"/api/documents/{doc_id}/text", params={"source": "ocr"}).json()
    assert all(p["text"] is None for p in ocr_text["pages"])

    cmp = client.get(f"/api/documents/{doc_id}/compare", params={"a": src, "b": "native"}).json()
    assert cmp["overall"]["pages_scored"] == 2
    assert cmp["pages"][0]["metrics"]["cer"] < 0.05
    # page 2 is a scan: native text is empty, so almost everything is "missing"
    assert cmp["pages"][1]["metrics"]["cer"] > 0.9
    assert cmp["overall"]["worst_page"] == 1
    none = client.get(f"/api/documents/{doc_id}/compare", params={"a": src, "b": "ocr"}).json()
    assert none["overall"] is None and none["pages"][0]["missing"] == ["ocr"]

    for fmt in ("txt", "md", "json", "jsonl"):
        r = client.get(f"/api/documents/{doc_id}/export", params={"source": src, "fmt": fmt})
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = client.get(f"/api/documents/{doc_id}/export", params={"source": src, "fmt": "jsonl"}).text.splitlines()
    assert [json.loads(line)["page"] for line in lines] == [1, 2]
    assert "## Page 2" in client.get(f"/api/documents/{doc_id}/export", params={"source": src, "fmt": "md"}).text


def test_job_validation(client, doc_id):
    url = f"/api/documents/{doc_id}/jobs"
    assert client.post(url, json={"kind": "llm", "llm": {"provider": "anthropic"}}).status_code == 400  # no key
    assert client.post(url, json={"kind": "llm"}).status_code == 400
    assert client.post(url, json={"kind": "llm", "pages": "9", "llm": {"provider": "fake"}}).status_code == 400
    assert client.get("/api/jobs/nope").status_code == 404


def test_library_search_and_provider_test(client, doc_id):
    res = client.get("/api/search", params={"q": "lazy dog"}).json()
    assert res["total"] == 1 and res["documents"][0]["document"]["id"] == doc_id
    r = client.post("/api/providers/fake/test", json={}).json()
    assert r["ok"] is False and "lazzy" in r["text"]  # the fake always returns the invoice text
    assert client.post("/api/providers/nope/test", json={}).status_code == 404
    docs = client.get("/api/documents", params={"details": True}).json()
    assert docs[0]["ocr_pages"] == 0 and docs[0]["llm_pages"] == 0


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_ocr_job(client, doc_id):
    job = client.post(f"/api/documents/{doc_id}/jobs", json={"kind": "ocr", "pages": "2"}).json()
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done" and job["total"] == 1
    sources = {s["id"]: s for s in client.get(f"/api/documents/{doc_id}/sources").json()}
    assert sources["ocr"]["pages"] == [1]


# ---------------------------------------------------------------- CLI
def test_cli_init_and_doctor(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    cli.get_settings.cache_clear()
    with pytest.raises(SystemExit) as e:
        cli.main(["init"])
    assert e.value.code == 0 and "ANTHROPIC_API_KEY=" in (tmp_path / ".env").read_text()
    with pytest.raises(SystemExit) as e:
        cli.main(["init"])  # refuses to overwrite
    assert e.value.code == 1
    with pytest.raises(SystemExit):
        cli.main(["doctor"])
    assert "LLM providers" in capsys.readouterr().out
    cli.get_settings.cache_clear()


def test_cli_extract_native(tmp_path, monkeypatch, sample_pdf, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    cli.get_settings.cache_clear()
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(sample_pdf)
    out = tmp_path / "out.jsonl"
    with pytest.raises(SystemExit) as e:
        cli.main(["extract", str(pdf), "--engine", "native", "--format", "jsonl", "-o", str(out)])
    assert e.value.code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 2 and "quick brown fox" in rows[0]["text"]
    with pytest.raises(SystemExit) as e:
        cli.main(["extract", str(pdf), "--engine", "native", "--pages", "1", "--format", "md"])
    assert "## Page 1" in capsys.readouterr().out
    cli.get_settings.cache_clear()


def test_env_template_in_sync():
    root = Path(__file__).resolve().parents[1]
    assert (root / ".env.example").read_text() == (root / "src/pyocr_toolkit/env.example").read_text()


def test_job_timing_and_eta():
    job = Job(id="j", doc_id="d", kind="llm", pages=list(range(10)), label="x", status="running", started_at=100.0)
    assert job.timing(now=110.0) == (None, None)  # nothing has really run yet
    job.done, job.cached = 4, 2  # cache hits are instant and don't count toward the rate
    assert job.timing(now=110.0) == (5.0, 30.0)  # 10s / 2 pages; 6 pages left
    job.status, job.finished_at, job.done = "done", 120.0, 10
    assert job.timing(now=999.0) == (2.5, None)  # no ETA once finished; uses finished_at
    data = job.to_json()
    assert data["seconds_per_page"] == 2.5 and data["eta_seconds"] is None and data["waiting_pages"] == []


def test_llm_job_waits_for_a_slot(tmp_path, sample_pdf):
    """Pages queued behind LLM_CONCURRENCY are 'waiting', not 'working on'."""

    async def scenario():
        settings = Settings(data_dir=tmp_path, llm_concurrency=1, _env_file=None)
        services = Services(settings, {"fake": FakeProvider()})
        store = DocumentStore(settings.documents_dir)
        doc_id = store.add("sample.pdf", sample_pdf).id
        manager = JobManager(store, services)
        async with services.llm_semaphore:  # something else holds the only slot
            job = manager.start(doc_id, "llm", [0, 1], llm=LLMSpec(provider="fake"))
            for _ in range(100):
                if job.waiting_pages:
                    break
                await asyncio.sleep(0.01)
            assert job.waiting_pages == [0] and job.current_pages == []
            assert job.to_json()["waiting_pages"] == [0]
        await manager._tasks[job.id]
        assert job.status == "done" and job.done == 2 and not job.waiting_pages and not job.current_pages

    asyncio.run(scenario())


def test_llm_cost_in_usd_and_inr(tmp_path):
    services = Services(Settings(data_dir=tmp_path, usd_inr_rate=90.0, _env_file=None), {})
    # claude-opus-5-5 lists at $4 in / $20 out per 1M tokens; dated snapshots match loosely
    cost = services.cost("anthropic", "claude-opus-5-5-20261001", 1_000_000, 100_000)
    assert cost["usd"] == 6.0 and cost["inr"] == 540.0 and cost["price_model"] == "claude-opus-5-5"
    assert services.cost("fake", "fake-1", 10, 20) is None  # not in the catalog: no made-up price
    stored = {"provider": "anthropic", "model": "weird-alias", "requested_model": "claude-opus-5-5",
              "input_tokens": 500_000, "output_tokens": 0}
    assert services.with_cost(stored)["cost"]["usd"] == 2.0  # falls back to the requested model


def test_job_cost_projection():
    job = Job(id="j", doc_id="d", kind="llm", pages=list(range(10)), label="x", status="running",
              price={"in": 4, "out": 20, "model": "claude-opus-5-5", "usd_inr_rate": 90.0})
    job.done, job.cached, job.failed = 5, 1, 0  # 4 paid pages
    job.input_tokens, job.output_tokens = 200_000, 20_000  # $0.80 + $0.40
    cost = job.cost()
    assert round(cost["usd"], 6) == 1.2 and round(cost["inr"], 6) == 108.0
    assert round(cost["projected_usd"], 6) == 2.7  # $0.30/page * (4 done + 5 left)
    job.status = "done"
    assert job.cost()["projected_usd"] is None
    assert Job(id="o", doc_id="d", kind="ocr", pages=[0], label="x").to_json()["cost"] is None


def test_llm_results_carry_cost(client, doc_id):
    r = client.post(f"/api/documents/{doc_id}/pages/0/llm", json={"provider": "fake"}).json()
    assert "cost" in r and r["cost"] is None  # the fake model has no catalog price
    assert client.get(f"/api/documents/{doc_id}/pages/0/llm").json()[0]["cost"] is None
