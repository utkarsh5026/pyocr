"""OCR engine registry, geometry helpers, engine parsers and the multi-engine API.

No engine library or network is needed: cloud engines run against
``httpx.MockTransport``, local engines against tiny fake modules, and the API
flow uses ``FakeOCREngine``, which "reads" fixed boxes from any image.
"""

from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pyocr_toolkit.api import create_app
from pyocr_toolkit.brands import BRANDS
from pyocr_toolkit.cli import build_parser
from pyocr_toolkit.config import Settings
from pyocr_toolkit.documents import RenderedImage, Word
from pyocr_toolkit.llm import ProviderError, build_providers
from pyocr_toolkit.ocr import OCR_ENGINE_CLASSES, OCREngine, OCRResult, build_ocr_engines
from pyocr_toolkit.ocr.base import split_line, words_from_boxes, words_to_text
from pyocr_toolkit.ocr.cloud import (
    AzureDocumentIntelligenceEngine,
    GoogleVisionEngine,
    MistralOCREngine,
    TextractEngine,
    parse_textract,
)
from pyocr_toolkit.ocr.local import EasyOCREngine, RapidOCREngine, parse_doctr, parse_paddleocr

from .conftest import HAS_TESSERACT

STATIC = Path(__file__).resolve().parents[1] / "src" / "pyocr_toolkit" / "static"
IMG = RenderedImage(b"\x89PNGfake", "image/png", 1000, 500, 500.0, 250.0)


class FakeOCREngine(OCREngine):
    """Reads two lines of the sample PDF's first page, as unordered line boxes in pixels."""

    id = "fakeocr"
    label = "Fake OCR"
    brand = "easyocr"

    def __init__(self, settings):
        super().__init__(settings)
        self.calls = 0

    def recognize(self, image: RenderedImage) -> OCRResult:
        self.calls += 1
        w = image.width_px
        boxes = [  # deliberately out of order: the second line first
            (0.12 * w, 300, 0.9 * w, 340, "The quick brown fox jumps over the lazy dog."),
            (0.12 * w, 250, 0.5 * w, 290, "Invoice 2024-117"),
        ]
        return OCRResult(words=words_from_boxes(boxes), model="fake-v1", warnings=["just a test"])


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    app = create_app(settings)
    fake = FakeOCREngine(settings)
    app.state.services.ocr_engines["fakeocr"] = fake
    with TestClient(app) as c:
        c.fake_ocr = fake
        c.data_dir = tmp_path
        yield c


@pytest.fixture
def doc_id(client, sample_pdf):
    r = client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- registry
def test_registry_ids_and_brands():
    ids = [cls.id for cls in OCR_ENGINE_CLASSES]
    assert ids[0] == "tesseract" and len(ids) == len(set(ids)) == 9
    for cls in OCR_ENGINE_CLASSES:
        assert cls.brand in BRANDS, cls.id
    for p in build_providers(Settings(_env_file=None)).values():
        assert p.brand in BRANDS, p.id


def test_brand_icons_are_bundled():
    for key, b in BRANDS.items():
        if b.icon:
            assert (STATIC / "logos" / b.icon).is_file(), key


def test_unconfigured_engines_say_what_to_do(monkeypatch):
    engines = build_ocr_engines(Settings(_env_file=None))
    assert "GOOGLE_VISION_API_KEY" in engines["google_vision"].problem()
    assert "AZURE_DOCUMENT_INTELLIGENCE_KEY" in engines["azure_di"].problem()
    assert "MISTRAL_API_KEY" in engines["mistral"].problem()
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert 'pip install "pyocr-toolkit[easyocr]"' in engines["easyocr"].problem()


# ---------------------------------------------------------------- geometry
def test_split_line_shares_width_by_characters():
    words = split_line(0, 10, 110, 20, "ab cdefgh", block=0, line=3)
    assert [w.text for w in words] == ["ab", "cdefgh"]
    assert words[0].x0 == 0 and words[1].x1 == pytest.approx(110)
    assert words[0].x1 < words[1].x0  # a gap for the space
    assert all(w.line == 3 for w in words)


def test_words_from_boxes_rebuilds_reading_order():
    boxes = [(300, 102, 400, 118, "right"), (10, 200, 90, 220, "second line"), (10, 100, 200, 120, "left part")]
    text = words_to_text(words_from_boxes(boxes))
    assert text == "left part right\nsecond line\n"


def test_words_to_text_separates_blocks():
    words = [Word(0, 0, 1, 1, "a", 0, 0), Word(0, 0, 1, 1, "b", 0, 0), Word(0, 0, 1, 1, "c", 0, 1),
             Word(0, 0, 1, 1, "d", 1, 2)]
    assert words_to_text(words) == "a b\nc\n\nd\n"


# ---------------------------------------------------------------- local engine parsers
def test_easyocr_engine_with_fake_module(monkeypatch):
    seen = {}

    class Reader:
        def __init__(self, langs, gpu, verbose):
            seen["langs"], seen["gpu"] = langs, gpu

        def readtext(self, data, detail, paragraph):
            seen["data"] = data
            return [([[10, 50], [90, 50], [90, 70], [10, 70]], "World", 0.9),
                    ([[10, 10], [90, 10], [90, 30], [10, 30]], "Hello", 0.8)]

    monkeypatch.setitem(sys.modules, "easyocr", types.SimpleNamespace(Reader=Reader))
    engine = EasyOCREngine(Settings(_env_file=None, easyocr_languages="en, de"))
    result = engine.recognize(IMG)
    assert seen == {"langs": ["en", "de"], "gpu": False, "data": IMG.data}
    assert words_to_text(result.words) == "Hello\nWorld\n"


def test_rapidocr_both_result_shapes(monkeypatch):
    box = [[0, 0], [50, 0], [50, 10], [0, 10]]
    new_style = types.SimpleNamespace(boxes=[box], txts=("new",), scores=(0.9,))
    old_style = ([[box, "old", 0.9]], [0.1, 0.2])
    for out, expected in ((new_style, "new"), (old_style, "old"), ((None, None), "")):
        mod = types.SimpleNamespace(RapidOCR=lambda o=out: (lambda data: o))
        monkeypatch.setitem(sys.modules, "rapidocr", mod)
        assert words_to_text(RapidOCREngine(Settings(_env_file=None)).recognize(IMG).words).strip() == expected


def test_paddleocr_parser_handles_v2_and_v3():
    poly = [[0, 0], [40, 0], [40, 10], [0, 10]]
    v3 = [{"rec_texts": ["three"], "rec_polys": [poly]}]
    v2 = [[[poly, ("two", 0.99)]]]
    assert parse_paddleocr(v3) == [(0.0, 0.0, 40.0, 10.0, "three")]
    assert parse_paddleocr(v2) == [(0.0, 0.0, 40.0, 10.0, "two")]
    assert parse_paddleocr([None]) == []


def test_doctr_parser_scales_relative_geometry():
    word = lambda v, g: types.SimpleNamespace(value=v, geometry=g)  # noqa: E731
    line = lambda *ws: types.SimpleNamespace(words=list(ws))  # noqa: E731
    page = types.SimpleNamespace(blocks=[
        types.SimpleNamespace(lines=[line(word("Hi", ((0.1, 0.1), (0.2, 0.2))), word("there", ((0.25, 0.1), (0.4, 0.2))))]),
        types.SimpleNamespace(lines=[line(word("Bye", ((0.1, 0.5), (0.2, 0.6))))]),
    ])
    words = parse_doctr(types.SimpleNamespace(pages=[page]), 1000, 500)
    assert words[0].rect == [100.0, 50.0, 200.0, 100.0]
    assert words_to_text(words) == "Hi there\n\nBye\n"


# ---------------------------------------------------------------- cloud engines (mocked HTTP)
def _mock(engine, handler):
    engine._http = httpx.Client(transport=httpx.MockTransport(handler))
    return engine


def test_google_vision_request_and_parse():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["key"] = str(request.url), request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        sym = lambda t, brk=None: {"text": t, **({"property": {"detectedBreak": {"type": brk}}} if brk else {})}  # noqa: E731
        return httpx.Response(200, json={"responses": [{"fullTextAnnotation": {
            "text": "Hello world\nBye\n",
            "pages": [{"blocks": [{"paragraphs": [{"words": [
                {"boundingBox": {"vertices": [{}, {"x": 40}, {"x": 40, "y": 10}, {"y": 10}]},
                 "symbols": [sym("H"), sym("ello", "SPACE")]},
                {"boundingBox": {"vertices": [{"x": 50}, {"x": 90}, {"x": 90, "y": 10}, {"x": 50, "y": 10}]},
                 "symbols": [sym("world", "EOL_SURE_SPACE")]},
                {"boundingBox": {"vertices": [{"y": 20}, {"x": 30, "y": 20}, {"x": 30, "y": 30}, {"y": 30}]},
                 "symbols": [sym("Bye", "LINE_BREAK")]},
            ]}]}]}],
        }}]})

    engine = _mock(GoogleVisionEngine(Settings(_env_file=None, google_vision_api_key="gk",
                                               google_vision_language_hints="en,de")), handler)
    result = engine.recognize(IMG)
    assert seen["url"] == GoogleVisionEngine.url and seen["key"] == "gk"
    req = seen["body"]["requests"][0]
    assert req["features"] == [{"type": "DOCUMENT_TEXT_DETECTION"}]
    assert req["imageContext"] == {"languageHints": ["en", "de"]}
    assert base64.b64decode(req["image"]["content"]) == IMG.data
    assert result.text == "Hello world\nBye\n"
    assert [w.text for w in result.words] == ["Hello", "world", "Bye"]
    assert result.words[0].rect == [0.0, 0.0, 40.0, 10.0]  # omitted x/y mean 0
    assert result.words[0].line == result.words[1].line != result.words[2].line


def test_google_vision_errors_are_readable():
    def handler(request):
        return httpx.Response(403, json={"error": {"message": "API key not valid"}})

    engine = _mock(GoogleVisionEngine(Settings(_env_file=None, google_vision_api_key="bad")), handler)
    with pytest.raises(ProviderError, match="API key not valid.*GOOGLE_VISION_API_KEY"):
        engine.recognize(IMG)


def _az_word(text, polygon, offset):
    return {"content": text, "polygon": polygon, "span": {"offset": offset, "length": len(text)}, "confidence": 0.99}


def test_azure_document_intelligence_polls_and_parses():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        assert request.headers["Ocp-Apim-Subscription-Key"] == "ak"
        if request.method == "POST":
            assert json.loads(request.content) == {"base64Source": base64.b64encode(IMG.data).decode()}
            return httpx.Response(202, headers={"Operation-Location": "https://az.test/op/1", "Retry-After": "0"})
        if len(calls) == 2:
            return httpx.Response(200, json={"status": "running"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"status": "succeeded", "analyzeResult": {
            "modelId": "prebuilt-read", "content": "Hi there\nBye",
            "pages": [{"width": 500, "height": 250, "unit": "pixel",
                       "lines": [{"content": "Hi there", "spans": [{"offset": 0, "length": 8}]},
                                 {"content": "Bye", "spans": [{"offset": 9, "length": 3}]}],
                       "words": [_az_word("Hi", [10, 10, 30, 10, 30, 20, 10, 20], 0),
                                 _az_word("there", [35, 10, 80, 10, 80, 20, 35, 20], 3),
                                 _az_word("Bye", [10, 30, 40, 30, 40, 40, 10, 40], 9)]}],
        }})

    engine = _mock(AzureDocumentIntelligenceEngine(Settings(
        _env_file=None, azure_document_intelligence_endpoint="https://az.test/", azure_document_intelligence_key="ak")),
        handler)
    result = engine.recognize(IMG)
    assert calls[0] == ("POST", "https://az.test/documentintelligence/documentModels/prebuilt-read:analyze"
                                "?api-version=2024-11-30")
    assert [c[0] for c in calls] == ["POST", "GET", "GET"]
    assert result.text == "Hi there\nBye" and result.model == "prebuilt-read"
    # the result page is 500 px wide but the image is 1000 px: boxes are scaled up
    assert result.words[0].rect == [20.0, 20.0, 60.0, 40.0]
    assert [w.line for w in result.words] == [0, 0, 1]


def test_azure_failed_analysis():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, headers={"Operation-Location": "https://az.test/op/1"})
        return httpx.Response(200, json={"status": "failed", "error": {"message": "InvalidImage"}})

    engine = _mock(AzureDocumentIntelligenceEngine(Settings(
        _env_file=None, azure_document_intelligence_endpoint="https://az.test", azure_document_intelligence_key="k")),
        handler)
    with pytest.raises(ProviderError, match="failed: InvalidImage"):
        engine.recognize(IMG)


def test_mistral_ocr_request_and_parse():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "mistral-ocr-2512", "pages": [{"index": 0, "markdown": "# Title\n\nBody"}],
                                         "usage_info": {"pages_processed": 1}})

    engine = _mock(MistralOCREngine(Settings(_env_file=None, mistral_api_key="mk")), handler)
    result = engine.recognize(IMG)
    assert seen["auth"] == "Bearer mk"
    assert seen["body"]["model"] == "mistral-ocr-latest"
    assert seen["body"]["document"] == {"type": "image_url",
                                        "image_url": "data:image/png;base64," + base64.b64encode(IMG.data).decode()}
    assert result.text == "# Title\n\nBody\n" and result.words == [] and result.model == "mistral-ocr-2512"


def test_textract_parses_lines_and_words():
    resp = {"DetectDocumentTextModelVersion": "1.0", "Blocks": [
        {"Id": "p", "BlockType": "PAGE"},
        {"Id": "l1", "BlockType": "LINE", "Text": "Hello world",
         "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.5, "Height": 0.1}},
         "Relationships": [{"Type": "CHILD", "Ids": ["w1", "w2"]}]},
        {"Id": "w1", "BlockType": "WORD", "Text": "Hello",
         "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.2, "Height": 0.1}}},
        {"Id": "w2", "BlockType": "WORD", "Text": "world",
         "Geometry": {"BoundingBox": {"Left": 0.35, "Top": 0.1, "Width": 0.25, "Height": 0.1}}},
        {"Id": "l2", "BlockType": "LINE", "Text": "no children",
         "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.3, "Width": 0.4, "Height": 0.1}}},
    ]}
    result = parse_textract(resp, 1000, 500)
    assert result.text == "Hello world\nno children\n"
    assert [w.text for w in result.words] == ["Hello", "world", "no", "children"]
    assert result.words[0].rect == [100.0, 50.0, 300.0, 100.0]
    assert [w.line for w in result.words] == [0, 0, 1, 1]


def test_textract_engine_uses_the_client_and_wraps_errors(monkeypatch):
    class Client:
        def __init__(self, fail):
            self.fail = fail

        def detect_document_text(self, Document):  # noqa: N803 - boto3's argument name
            if self.fail:
                raise RuntimeError("Unable to locate credentials")
            assert Document == {"Bytes": IMG.data}
            return {"Blocks": []}

    engine = TextractEngine(Settings(_env_file=None, aws_region="us-east-1"))
    engine._model = Client(fail=False)
    assert engine.recognize(IMG).words == []
    engine._model = Client(fail=True)
    with pytest.raises(ProviderError, match="AWS_ACCESS_KEY_ID"):
        engine.recognize(IMG)


# ---------------------------------------------------------------- API with a fake engine
def test_config_lists_engines_and_brands(client):
    cfg = client.get("/api/config").json()
    ids = [e["id"] for e in cfg["ocr_engines"]]
    assert ids[:2] == ["tesseract", "rapidocr"] and "fakeocr" in ids
    assert cfg["brands"]["claude"]["icon"] == "claude.svg"
    assert {p["brand"] for p in cfg["providers"]} <= set(cfg["brands"])


def test_ocr_with_another_engine(client, doc_id):
    r = client.get(f"/api/documents/{doc_id}/pages/0/text/ocr?engine=fakeocr&cached_only=true")
    assert r.status_code == 200 and r.json() is None

    layer = client.get(f"/api/documents/{doc_id}/pages/0/text/ocr?engine=fakeocr").json()
    assert layer["source"] == "ocr:fakeocr"
    assert layer["text"] == "Invoice 2024-117\nThe quick brown fox jumps over the lazy dog.\n"
    assert layer["meta"]["model"] == "fake-v1" and layer["meta"]["warnings"] == ["just a test"]
    # boxes are in PDF points: inside the 595 x 842 pt page, not in 300-DPI pixels
    assert all(0 <= x0 < x1 <= 595 and 0 <= y0 < y1 <= 842 for x0, y0, x1, y1, *_ in layer["words"])
    assert (client.data_dir / "documents" / doc_id / "ocr" / "fakeocr" / "0.json").exists()

    # cached: the engine is not called again
    client.get(f"/api/documents/{doc_id}/pages/0/text/ocr?engine=fakeocr")
    assert client.fake_ocr.calls == 1

    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    assert pages[0]["ocr_engines"] == ["fakeocr"] and not pages[0]["has_ocr_cache"]

    sources = {s["id"]: s for s in client.get(f"/api/documents/{doc_id}/sources").json()}
    assert sources["ocr:fakeocr"]["pages"] == [0] and sources["ocr:fakeocr"]["label"] == "Fake OCR"
    assert sources["ocr"]["pages"] == []  # Tesseract is still listed

    cmp = client.post(f"/api/documents/{doc_id}/pages/0/compare", json={
        "reference": {"source": "native"}, "hypothesis": {"source": "ocr", "engine": "fakeocr"},
        "ignore_case": True}).json()
    assert cmp["hypothesis"]["label"] == "Fake OCR"
    # the fake reads two of the page's three lines: roughly a quarter of the characters are missing
    assert 0.1 < cmp["metrics"]["cer"] < 0.4

    found = client.get(f"/api/documents/{doc_id}/search?q=brown fox&sources=ocr").json()
    hit = found["pages"][0]["hits"][0]
    assert hit["engine"] == "fakeocr" and hit["boxes"]

    export = client.get(f"/api/documents/{doc_id}/export?source=ocr:fakeocr&fmt=txt")
    assert "Invoice 2024-117" in export.text
    assert 'filename="sample.ocr-fakeocr.txt"' in export.headers["content-disposition"]

    assert client.get(f"/api/documents/{doc_id}").json()["ocr_pages"] == 1


def test_unknown_and_unavailable_engines(client, doc_id):
    r = client.get(f"/api/documents/{doc_id}/pages/0/text/ocr?engine=nope")
    assert r.status_code == 404 and "unknown OCR engine" in r.json()["detail"]  # "unknown ..." errors are 404s
    r = client.get(f"/api/documents/{doc_id}/pages/0/text/ocr?engine=mistral")
    assert r.status_code == 503 and "MISTRAL_API_KEY" in r.json()["detail"]
    r = client.post(f"/api/documents/{doc_id}/jobs", json={"kind": "ocr", "engine": "google_vision"})
    assert r.status_code == 400 and "GOOGLE_VISION_API_KEY" in r.json()["detail"]
    assert client.post("/api/ocr-engines/nope/test").status_code == 404


def test_ocr_job_with_engine(client, doc_id):
    import time

    job = client.post(f"/api/documents/{doc_id}/jobs", json={"kind": "ocr", "engine": "fakeocr"}).json()
    assert job["engine"] == "fakeocr" and job["label"].startswith("Fake OCR")
    for _ in range(100):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["done"] == 2 and job["failed"] == 0
    assert client.fake_ocr.calls == 2


def test_engine_test_endpoint(client):
    r = client.post("/api/ocr-engines/fakeocr/test").json()
    assert r["model"] == "fake-v1" and "Invoice" in r["text"]


def test_old_tesseract_cache_is_still_read(client, doc_id):
    """Results written before multi-engine OCR (no ``meta``, in ocr/<page>.json) keep working."""
    old = {"source": "ocr", "text": "old tesseract text", "elapsed_ms": 12.0,
           "words": [[72.0, 90.0, 100.0, 104.0, "old", 0, 0]]}
    folder = client.data_dir / "documents" / doc_id / "ocr"
    folder.mkdir(parents=True)
    (folder / "1.json").write_text(json.dumps(old))
    layer = client.get(f"/api/documents/{doc_id}/pages/1/text/ocr?cached_only=true").json()
    assert layer["text"] == "old tesseract text" and layer["meta"] == {}
    pages = client.get(f"/api/documents/{doc_id}/pages").json()
    assert pages[1]["has_ocr_cache"] and pages[1]["ocr_engines"] == ["tesseract"]
    assert client.get(f"/api/documents/{doc_id}/text?source=ocr").json()["pages"][1]["text"] == "old tesseract text"
    assert client.get(f"/api/documents/{doc_id}/text?source=ocr:tesseract").json()["pages"][1]["text"] == \
        "old tesseract text"


def test_providers_page_shows_engines_without_secrets(tmp_path):
    secret = "SECRET-vision-key-123"
    app = create_app(Settings(data_dir=tmp_path, _env_file=None, google_vision_api_key=secret,
                              mistral_api_key="SECRET-mistral-456"))
    with TestClient(app) as c:
        html = c.get("/providers").text
        assert "OCR engines" in html and "Google Cloud Vision" in html and "AWS Textract" in html
        assert "/static/logos/googlecloud.svg" in html and 'class="logo' in html
        cfg = c.get("/api/config").text
        for text in (html, cfg):
            assert secret not in text and "SECRET-mistral-456" not in text
        engines = {e["id"]: e for e in c.get("/api/config").json()["ocr_engines"]}
        assert engines["google_vision"]["available"] and engines["mistral"]["available"]


def test_cli_accepts_ocr_engine_source():
    from pyocr_toolkit.cli import _ocr_engine_arg

    args = build_parser().parse_args(["extract", "x.pdf", "--engine", "ocr:easyocr"])
    assert _ocr_engine_arg(args) == "easyocr"
    args = build_parser().parse_args(["extract", "x.pdf", "--engine", "ocr", "--ocr-engine", "doctr"])
    assert _ocr_engine_arg(args) == "doctr"


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_tesseract_on_two_documents_at_once(tmp_path, sample_pdf, sample_png):
    """Regression: Leptonica is process-wide, so OCR on *different* documents must not overlap."""
    from concurrent.futures import ThreadPoolExecutor

    from pyocr_toolkit.documents import DocumentStore

    store = DocumentStore(tmp_path)
    docs = [store.get(store.add("a.pdf", sample_pdf).id), store.get(store.add("b.png", sample_png).id)]
    jobs = [(docs[0], 1), (docs[1], 0)] * 2
    with ThreadPoolExecutor(4) as pool:
        layers = list(pool.map(lambda job: job[0].ocr_text(job[1], dpi=200, language="eng", force=True), jobs))
    assert all(layer.text.strip() for layer in layers)


def test_engine_credentials_saved_from_ui(client):
    """Cloud OCR keys can be set on the Providers page like LLM keys, and are never echoed back."""
    secret = "SECRET-gv-789"
    r = client.post("/api/ocr-engines/google_vision/credentials",
                    json={"values": {"GOOGLE_VISION_API_KEY": secret, "GOOGLE_VISION_LANGUAGE_HINTS": "en,de"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"]["configured"] and secret not in r.text
    hints = next(e for e in body["settings"] if e["name"] == "GOOGLE_VISION_LANGUAGE_HINTS")
    assert hints["saved"] and hints["value"] == "en,de"  # not a secret, so it is shown
    engines = {e["id"]: e for e in client.get("/api/config").json()["ocr_engines"]}
    assert engines["google_vision"]["available"]
    assert secret not in client.get("/providers").text
    assert secret in (client.data_dir / "credentials.json").read_text()

    # settings that belong to another engine, or aren't text, are refused
    r = client.post("/api/ocr-engines/google_vision/credentials", json={"values": {"MISTRAL_API_KEY": "x"}})
    assert r.status_code == 400
    r = client.post("/api/ocr-engines/easyocr/credentials", json={"values": {"OCR_GPU": "false"}})
    assert r.status_code == 400
    assert client.post("/api/ocr-engines/nope/credentials", json={"values": {}}).status_code == 404
    r = client.post("/api/ocr-engines/mistral/credentials", json={"values": {"MISTRAL_API_KEY": "x"}},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403

    r = client.post("/api/ocr-engines/google_vision/credentials", json={"clear": ["GOOGLE_VISION_API_KEY"]})
    assert not r.json()["provider"]["configured"]


def test_saving_engine_settings_resets_its_client(client):
    engine = client.app.state.services.ocr_engines["mistral"]
    engine._http, engine._model = object(), object()
    client.post("/api/ocr-engines/mistral/credentials", json={"values": {"MISTRAL_API_KEY": "k"}})
    assert engine._http is None and engine._model is None
