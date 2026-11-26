"""Image preprocessing: spec parsing, pixel ops, cache keys and the API / job / CLI wiring."""

from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pyocr_toolkit import cli
from pyocr_toolkit.api import create_app
from pyocr_toolkit.config import Settings
from pyocr_toolkit.documents import OpenDocument
from pyocr_toolkit.preprocess import PRESETS, Preprocess, otsu_level, process_bytes

from .test_api import FakeProvider
from .test_web import wait_for_job


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


def _img(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


# ---------------------------------------------------------------- spec
def test_spec_is_normalised():
    # order, spacing, trailing zeros and implied steps don't create different specs
    assert str(Preprocess.parse(" contrast=1.50 ; gray ")) == "gray;contrast=1.5"
    assert str(Preprocess.parse("threshold")) == "gray;threshold=otsu"  # binarize implies grayscale
    assert str(Preprocess.parse("rotate=-90;crop=5,0,5,0")) == "rotate=270;crop=5,0,5,0"
    assert str(Preprocess.parse("contrast=1;brightness=1")) == ""  # 1 = unchanged
    assert Preprocess.parse("").is_identity and Preprocess.parse(None).is_identity
    assert not Preprocess.parse("dpi=300").changes_pixels and Preprocess.parse("gray").changes_pixels
    assert Preprocess.parse("clean") == Preprocess.parse(PRESETS["clean"]["spec"])
    assert Preprocess.parse("gray;contrast=1.5").describe() == "grayscale, contrast ×1.5"


def test_preset_specs_are_canonical():
    # the UI matches presets by comparing canonical strings, so presets must already be canonical
    for name, preset in PRESETS.items():
        assert str(Preprocess.parse(preset["spec"])) == preset["spec"], name


@pytest.mark.parametrize("bad", ["sepia", "contrast=9", "rotate=45", "crop=1,2", "crop=45,0,45,0", "dpi=10",
                                 "denoise=4", "threshold=300", "gray=maybe"])
def test_bad_specs_are_rejected(bad):
    with pytest.raises(ValueError):
        Preprocess.parse(bad)


# ---------------------------------------------------------------- pixels
def _sample(w=200, h=100) -> Image.Image:
    img = Image.new("RGB", (w, h), (225, 220, 210))  # off-white "paper"
    for x in range(20, 180):
        for y in range(40, 50):
            img.putpixel((x, y), (70, 70, 80))  # grey "ink"
    return img


def test_pixel_ops():
    img = _sample()
    assert Preprocess.parse("gray").apply(img).mode == "L"
    assert Preprocess.parse("rotate=90").apply(img).size == (100, 200)
    assert Preprocess.parse("crop=10,25,10,25").apply(img).size == (100, 80)
    bw = Preprocess.parse("threshold=otsu").apply(img)
    hist = bw.histogram()
    assert hist[0] and hist[255] and not sum(hist[1:255])  # only pure black and pure white
    assert 70 <= otsu_level(img) < 220  # splits ink from paper
    stretched = Preprocess.parse("gray;auto").apply(img)
    assert stretched.getextrema() == (0, 255)  # levels stretched to full range
    assert Preprocess.parse("gray;invert").apply(img).getpixel((0, 0)) < 40  # paper turned dark


def test_process_bytes_encodes_png():
    buf = io.BytesIO()
    _sample().save(buf, "PPM")
    data, mime = process_bytes(buf.getvalue(), Preprocess.parse("bw"))
    assert mime == "image/png" and _img(data).size == (200, 100)


# ---------------------------------------------------------------- cache key
def test_cache_key_unchanged_without_preprocessing():
    """Results cached before preprocessing existed must keep their keys (no orphaned user data)."""
    old_digest = hashlib.sha256(b"fake\0fake-1\0prompt").hexdigest()[:16]
    assert OpenDocument.llm_cache_key("fake", "fake-1", "prompt") == f"fake__fake-1__{old_digest}"
    assert OpenDocument.llm_cache_key("fake", "fake-1", "prompt", "") == f"fake__fake-1__{old_digest}"
    assert OpenDocument.llm_cache_key("fake", "fake-1", "prompt", "gray") != f"fake__fake-1__{old_digest}"


# ---------------------------------------------------------------- API
def test_llm_run_with_preprocessing(client, doc_id):
    url = f"/api/documents/{doc_id}/pages/0/llm"
    plain = client.post(url, json={"provider": "fake"}).json()
    edited = client.post(url, json={"provider": "fake", "preprocess": "contrast=1.50;gray"}).json()
    assert plain["key"] != edited["key"] and edited["cached"] is False
    assert plain["preprocess"] is None and edited["preprocess"] == "gray;contrast=1.5"
    assert edited["preprocess_label"] == "grayscale, contrast ×1.5"
    # the model really got a different (grayscale) image
    sent_plain, sent_edited = client.fake.calls
    assert _img(sent_plain.image).mode == "RGB" and _img(sent_edited.image).mode == "L"

    # the same settings written differently hit the cache
    again = client.post(url, json={"provider": "fake", "preprocess": "gray;contrast=1.5"}).json()
    assert again["cached"] is True and again["key"] == edited["key"] and len(client.fake.calls) == 2

    labels = [s["label"] for s in client.get(f"/api/documents/{doc_id}/sources").json() if s["kind"] == "llm"]
    assert len(labels) == 2 and sum("image: grayscale" in lb for lb in labels) == 1

    # both runs are separate sources that can be diffed against each other
    cmp = client.post(f"/api/documents/{doc_id}/pages/0/compare", json={
        "reference": {"source": "llm", "key": plain["key"]}, "hypothesis": {"source": "llm", "key": edited["key"]}}).json()
    assert cmp["metrics"]["cer"] == 0


def test_llm_image_is_exactly_what_the_model_gets(client, doc_id):
    base = f"/api/documents/{doc_id}/pages/0"
    for spec in (None, "bw"):
        client.post(f"{base}/llm", json={"provider": "fake", "preprocess": spec, "force": True})
        params = {"preprocess": spec} if spec else {}
        r = client.get(f"{base}/llm-image", params=params)
        assert r.status_code == 200 and r.content == client.fake.calls[-1].image
        assert r.headers["content-type"] == client.fake.calls[-1].mime_type
    assert r.headers["x-preprocess"] == "gray%3Bthreshold%3Dotsu"


def test_preview_and_validation(client, doc_id):
    base = f"/api/documents/{doc_id}/pages/0"
    thumb = client.get(f"{base}/image", params={"dpi": 40, "preprocess": "rotate=90;gray"})
    assert thumb.status_code == 200 and _img(thumb.content).mode == "L"
    w, h = _img(thumb.content).size
    assert w > h  # A4 portrait page rotated to landscape

    assert client.get(f"{base}/image", params={"preprocess": "sepia"}).status_code == 400
    assert client.get(f"{base}/llm-image", params={"preprocess": "contrast=99"}).status_code == 400
    r = client.post(f"{base}/llm", json={"provider": "fake", "preprocess": "sepia"})
    assert r.status_code == 400 and "unknown preprocessing step" in r.json()["detail"]
    assert client.fake.calls == []

    ok = client.get("/api/preprocess", params={"spec": "faded"}).json()
    assert ok["spec"] == PRESETS["faded"]["spec"] and ok["label"]
    assert client.get("/api/preprocess", params={"spec": "rotate=45"}).status_code == 400
    presets = client.get("/api/config").json()["preprocess_presets"]
    assert presets[0]["id"] == "original" and {p["id"] for p in presets} == set(PRESETS)


def test_job_with_preprocessing(client, doc_id):
    body = {"kind": "llm", "llm": {"provider": "fake", "preprocess": "clean"}}
    job = client.post(f"/api/documents/{doc_id}/jobs", json=body).json()
    assert "image: grayscale, auto levels" in job["label"]
    job = wait_for_job(client, job["id"])
    assert job["status"] == "done" and job["done"] == 2
    assert all(_img(c.image).mode == "L" for c in client.fake.calls)
    bad = client.post(f"/api/documents/{doc_id}/jobs", json={"kind": "llm", "llm": {"provider": "fake", "preprocess": "x"}})
    assert bad.status_code == 400


def test_cli_accepts_preprocess():
    parser = cli.build_parser()
    assert parser.parse_args(["extract", "f.pdf", "--preprocess", "bw"]).preprocess == "bw"
    assert parser.parse_args(["compare", "f.pdf", "--preprocess", "gray;auto"]).preprocess == "gray;auto"
    assert parser.parse_args(["extract", "f.pdf"]).preprocess is None


def test_workspace_page_has_editor(client, doc_id):
    html = client.get(f"/documents/{doc_id}/pages/1").text
    assert 'id="adjust-panel"' in html and 'id="ab-llm"' in html
    assert 'name="preprocess"' in client.get(f"/documents/{doc_id}").text
