"""Several pages per LLM request: prompt, request shape, reply splitting, fallback, caching and jobs."""

from __future__ import annotations

import asyncio
import json

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from pyocr_toolkit.api import create_app
from pyocr_toolkit.config import Settings
from pyocr_toolkit.documents import DocumentStore
from pyocr_toolkit.llm import PROMPT_MODES, PageImage, TranscriptionRequest, TranscriptionResult, split_batch
from pyocr_toolkit.llm.gemini_provider import _image_parts
from pyocr_toolkit.service import LLMSpec, Services

from .test_api import FakeProvider
from .test_providers import _claude_provider, _claude_stream, _openai_http
from .test_web import wait_for_job


class BatchFake(FakeProvider):
    """Answers like a well-behaved model: one ``=== PAGE n ===`` section per labelled image.

    ``drop`` leaves those labels out of multi-page replies (a model that skipped a page);
    ``stop_reason`` makes multi-page replies look cut off by the output limit.
    """

    def __init__(self, drop: tuple[str, ...] = (), stop_reason: str | None = None):
        super().__init__()
        self.drop = drop
        self.stop_reason = stop_reason

    async def _transcribe(self, req, model):
        self.calls.append(req)
        multi = len(req.pages) > 1
        sections = [f"=== {p.label.upper()} ===\ntext of {p.label.lower()}" for p in req.pages
                    if not (multi and p.label in self.drop)]
        return TranscriptionResult(text="\n\n".join(sections), provider=self.id, model=model, latency_ms=0,
                                   input_tokens=1000 * len(req.pages) + 1, output_tokens=10 * len(req.pages),
                                   stop_reason=self.stop_reason if multi else "end_turn")


def _setup(tmp_path, sample_pdf, provider):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    services = Services(settings, {"fake": provider})
    store = DocumentStore(settings.documents_dir)
    return services, store.get(store.add("sample.pdf", sample_pdf).id)


# ----------------------------------------------------------------- splitting
def test_split_batch():
    assert split_batch("=== PAGE 3 ===\nfoo\n\n=== PAGE 4 ===\nbar\n", [3, 4]) == ({3: "foo", 4: "bar"}, [])
    # markdown-wrapped markers and a blank page still split
    assert split_batch("**=== Page 3 ===**\nfoo\n## === PAGE 4 ===\n", [3, 4]) == ({3: "foo", 4: ""}, [])
    # a missing page is reported, never filled with a neighbour's text
    texts, problems = split_batch("=== PAGE 3 ===\nfoo bar", [3, 4])
    assert texts == {3: "foo bar"} and problems == ["no marker for pages 4"]
    # a repeated or foreign marker makes that page untrustworthy
    texts, problems = split_batch("=== PAGE 3 ===\na\n=== PAGE 3 ===\nb\n=== PAGE 9 ===\nc", [3, 4])
    assert texts == {} and any("more than once" in p for p in problems) and any("not sent: 9" in p for p in problems)
    # no markers at all: only a one-page request can be taken whole
    assert split_batch("plain text", [3]) == ({3: "plain text"}, [])
    assert split_batch("plain text", [3, 4])[0] == {}
    assert "dropped" in split_batch("preamble\n=== PAGE 3 ===\nx", [3])[1][0]


# ------------------------------------------------------------- request shape
BATCH_REQ = TranscriptionRequest(
    image=b"a", mime_type="image/png", system_prompt="SYS", user_prompt="Transcribe each of these pages.",
    pages=[PageImage(b"a", "image/png", "Page 1"), PageImage(b"b", "image/jpeg", "Page 2")],
)


def test_claude_sends_labelled_images():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, content=_claude_stream("=== PAGE 1 ===\nx", "claude-opus-5-5"),
                               headers={"content-type": "text/event-stream"})

    asyncio.run(_claude_provider("claude-opus-5-5", handler).transcribe(BATCH_REQ))
    content = seen["body"]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "Page 1" and content[2]["text"] == "Page 2"
    assert content[3]["source"]["media_type"] == "image/jpeg"
    assert content[4]["text"] == "Transcribe each of these pages."


def test_openai_and_gemini_send_labelled_images():
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "gpt-5",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        })

    from pyocr_toolkit.llm.openai_provider import OpenAIProvider
    provider = OpenAIProvider(Settings(_env_file=None, openai_api_key="test", openai_model="gpt-5"))
    provider._client = openai.AsyncOpenAI(api_key="test", http_client=_openai_http(handler))
    asyncio.run(provider.transcribe(BATCH_REQ))
    parts = seen["body"]["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text", "text", "image_url", "text", "image_url"]
    assert parts[1]["text"] == "Page 1" and parts[4]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    gemini = _image_parts(BATCH_REQ)
    assert gemini[0] == "Page 1" and gemini[2] == "Page 2" and len(gemini) == 4
    # a single-page request is unchanged: one unlabelled image
    single = TranscriptionRequest(image=b"a", mime_type="image/png", system_prompt="S", user_prompt="U")
    assert len(_image_parts(single)) == 1


# ------------------------------------------------------------------ services
def test_batched_run_splits_caches_and_is_its_own_source(tmp_path, sample_pdf):
    fake = BatchFake()
    services, doc = _setup(tmp_path, sample_pdf, fake)
    spec = LLMSpec(provider="fake", pages_per_request=2)

    out = asyncio.run(services.run_llm_batch(doc, [0, 1], spec))
    assert len(fake.calls) == 1
    req = fake.calls[0]
    assert [p.label for p in req.pages] == ["Page 1", "Page 2"]
    assert "up to 2 pages" in req.system_prompt and req.max_output_tokens == 32000
    assert out[0]["text"] == "text of page 1" and out[1]["text"] == "text of page 2"
    # the shared request's tokens are split so the pages add up to what was paid
    assert out[0]["input_tokens"] + out[1]["input_tokens"] == 2001
    assert out[0]["batch"]["pages"] == [0, 1] and out[0]["pages_per_request"] == 2

    # a different key from one-page-per-request, which keeps its old key
    single_key = doc.llm_cache_key("fake", "fake-1", PROMPT_MODES["verbatim"]["system"])
    assert services.llm_key(doc, LLMSpec(provider="fake")) == single_key
    assert out[0]["key"] != single_key and out[0]["key"] != services.llm_key(doc, LLMSpec("fake", pages_per_request=3))

    # second run: served from cache, no call
    again = asyncio.run(services.run_llm_pages(doc, [0, 1], spec))
    assert all(r["cached"] for r in again.values()) and len(fake.calls) == 1
    # one page on its own with the same spec lands in the same source
    assert asyncio.run(services.run_llm_page(doc, 1, spec))["cached"]

    sources = [s for s in services.list_sources(doc) if s["kind"] == "llm"]
    assert len(sources) == 1 and sources[0]["label"].endswith("· 2 pages/request")
    assert services.page_text(doc, 0, sources[0]["id"]) == "text of page 1"


def test_batched_run_uses_preprocessed_images(tmp_path, sample_pdf):
    """Batching and Adjust image combine: same images as a one-page run, own key, both in the label."""
    from pyocr_toolkit.preprocess import Preprocess

    fake = BatchFake()
    services, doc = _setup(tmp_path, sample_pdf, fake)
    spec = LLMSpec(provider="fake", preprocess="gray;contrast=1.5", pages_per_request=2)
    out = asyncio.run(services.run_llm_batch(doc, [0, 1], spec))

    pre = Preprocess.parse(spec.preprocess)
    sent = fake.calls[0].pages
    for n, page in enumerate(sent):
        assert (page.image, page.mime_type) == services.llm_image(doc, n, pre)
    assert sent[0].image != services.llm_image(doc, 0)[0]  # really preprocessed

    plain_batched = services.llm_key(doc, LLMSpec(provider="fake", pages_per_request=2))
    plain_single = services.llm_key(doc, LLMSpec(provider="fake", preprocess=spec.preprocess))
    assert out[0]["key"] == services.llm_key(doc, spec) and out[0]["key"] not in (plain_batched, plain_single)
    assert out[0]["preprocess"] == str(pre) and out[0]["preprocess_label"]
    # the job planner and the batch call agree on the key, so a re-run is all cache hits
    cached, chunks = services.plan_llm_chunks(doc, [0, 1], spec)
    assert cached == [0, 1] and chunks == []

    label = next(s for s in services.list_sources(doc) if s["kind"] == "llm")["label"]
    assert "· image: " in label and label.endswith("· 2 pages/request")


def test_page_missing_from_reply_is_resent_alone(tmp_path, sample_pdf):
    fake = BatchFake(drop=("Page 2",))
    services, doc = _setup(tmp_path, sample_pdf, fake)
    out = asyncio.run(services.run_llm_batch(doc, [0, 1], LLMSpec(provider="fake", pages_per_request=4)))
    assert [len(c.pages) for c in fake.calls] == [2, 1]
    assert out[1]["text"] == "text of page 2" and "transcribed on its own" in out[1]["warnings"][0]
    assert out[1]["batch"]["retried_alone"] and not out[0]["batch"]["retried_alone"]
    # the retry's tokens are added to that page's share
    assert out[0]["input_tokens"] + out[1]["input_tokens"] == 2001 + 1001


def test_truncated_reply_resends_the_last_page(tmp_path, sample_pdf):
    fake = BatchFake(stop_reason="max_tokens")
    services, doc = _setup(tmp_path, sample_pdf, fake)
    out = asyncio.run(services.run_llm_batch(doc, [0, 1], LLMSpec(provider="fake", pages_per_request=2)))
    assert [len(c.pages) for c in fake.calls] == [2, 1]
    assert not out[0]["batch"]["retried_alone"] and out[1]["batch"]["retried_alone"]
    assert "output-token limit" in out[1]["warnings"][0]


def test_pages_per_request_is_validated(tmp_path, sample_pdf):
    services, doc = _setup(tmp_path, sample_pdf, BatchFake())
    for bad in (0, 21):
        with pytest.raises(ValueError, match="pages per request"):
            services.resolve_spec(LLMSpec(provider="fake", pages_per_request=bad))


# ---------------------------------------------------------------------- jobs
@pytest.fixture
def batch_client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, _env_file=None))
    fake = BatchFake()
    app.state.providers["fake"] = fake
    with TestClient(app) as c:
        c.fake = fake
        yield c


def test_batched_job_end_to_end(batch_client, sample_pdf):
    client = batch_client
    doc_id = client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf)}).json()["id"]
    body = {"kind": "llm", "llm": {"provider": "fake", "pages_per_request": 2}}
    job = wait_for_job(client, client.post(f"/api/documents/{doc_id}/jobs", json=body).json()["id"])
    assert job["status"] == "done" and job["done"] == 2 and job["failed"] == 0
    assert "2 pages/request" in job["label"] and job["input_tokens"] == 2001
    assert len(client.fake.calls) == 1 and len(client.fake.calls[0].pages) == 2

    # cached pages are skipped before chunking: no new request
    job2 = wait_for_job(client, client.post(f"/api/documents/{doc_id}/jobs", json=body).json()["id"])
    assert job2["cached"] == 2 and len(client.fake.calls) == 1

    # the batched source compares against a one-page-per-request run like any other source
    single = {"kind": "llm", "llm": {"provider": "fake"}}
    wait_for_job(client, client.post(f"/api/documents/{doc_id}/jobs", json=single).json()["id"])
    llm = [s for s in client.get(f"/api/documents/{doc_id}/sources").json() if s["kind"] == "llm"]
    assert len(llm) == 2 and {s["pages_per_request"] for s in llm} == {1, 2}
    batched = next(s for s in llm if s["pages_per_request"] == 2)
    cmp = client.get(f"/api/documents/{doc_id}/compare", params={"a": batched["id"], "b": "native"}).json()
    assert cmp["overall"]["pages_scored"] == 2

    url = f"/api/documents/{doc_id}/jobs"
    assert client.post(url, json={"kind": "llm", "llm": {"provider": "fake", "pages_per_request": 0}}).status_code == 422
    assert client.post(url, json={"kind": "llm", "llm": {"provider": "fake", "pages_per_request": 21}}).status_code == 422
    assert client.get("/api/config").json()["max_pages_per_request"] == 20


def test_failed_batch_marks_every_page(batch_client, sample_pdf):
    client = batch_client

    async def boom(req, model):
        from pyocr_toolkit.llm import ProviderError
        raise ProviderError("Fake: rate limited, try again shortly")

    client.fake._transcribe = boom
    doc_id = client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf)}).json()["id"]
    body = {"kind": "llm", "llm": {"provider": "fake", "pages_per_request": 5}}
    job = wait_for_job(client, client.post(f"/api/documents/{doc_id}/jobs", json=body).json()["id"])
    assert job["status"] == "failed" and job["failed"] == 2 and job["done"] == 2
    assert job["errors"] == ["pages 1, 2: Fake: rate limited, try again shortly"]
    assert not job["current_pages"] and not job["waiting_pages"]
