from __future__ import annotations

import pytest

from pyocr_toolkit import compare, search
from pyocr_toolkit.documents import DocumentError, DocumentStore, TextLayer, Word
from pyocr_toolkit.llm.anthropic_provider import _supports_effort

from .conftest import HAS_TESSERACT


# ---------------------------------------------------------------- compare
def test_score_perfect_and_whitespace_insensitive():
    s = compare.score("Hello   world\nagain", "Hello world again")
    assert s["cer"] == 0 and s["wer"] == 0 and s["accuracy"] == 1


def test_score_counts_errors():
    s = compare.score("the cat sat", "the bat sat")
    assert s["char_distance"] == 1
    assert s["word_distance"] == 1
    assert s["wer"] == pytest.approx(1 / 3, abs=1e-3)


def test_score_options():
    assert compare.score("Hello, World", "hello world")["cer"] > 0
    opts = compare.NormalizeOptions(ignore_case=True, ignore_punctuation=True)
    assert compare.score("Hello, World", "hello world", opts)["cer"] == 0


def test_score_ligatures_and_hyphenation():
    assert compare.score("ﬁnal recog-\nnition", "final recognition")["cer"] == 0


def test_score_empty_reference():
    assert compare.score("", "")["cer"] == 0
    assert compare.score("", "x")["cer"] == 1


def test_word_diff_segments():
    diff = compare.word_diff("one two three four", "one too three four five")
    ops = [d["op"] for d in diff]
    assert ops == ["equal", "replace", "equal", "insert"]
    assert diff[1] == {"op": "replace", "ref": "two", "hyp": "too"}
    assert diff[-1]["hyp"] == "five"


def test_word_diff_keeps_line_breaks():
    diff = compare.word_diff("a b\nc d", "a b\nc d")
    assert diff == [{"op": "equal", "ref": "a b\nc d", "hyp": "a b\nc d"}]


# ---------------------------------------------------------------- search
def _words(*texts):
    return [Word(i * 10, 0, i * 10 + 8, 10, t, 0, 0) for i, t in enumerate(texts)]


def test_find_boxes_single_and_phrase():
    words = _words("The", "quick", "brown", "fox.")
    assert len(search.find_boxes(words, "QUICK")) == 1
    assert len(search.find_boxes(words, "quick bro")) == 2
    assert len(search.find_boxes(words, "brown fox")) == 2
    assert search.find_boxes(words, "quick fox") == []


def test_snippets_span_line_breaks():
    snips = search.find_snippets("alpha beta\ngamma delta", "beta gamma")
    assert len(snips) == 1 and snips[0]["match"] == "beta gamma"
    assert search.count_matches("a A a", "a") == 3


# ---------------------------------------------------------------- effort gating
@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5-5", True),
    ("claude-sonnet-5-5", True),
    ("claude-haiku-5-5", True),
    ("claude-fable-5-2", True),
    ("claude-opus-4-6", True),
    ("claude-opus-4-5", False),
    ("claude-haiku-4-5", False),
    ("claude-sonnet-4-5-20250929", False),
    ("my-foundry-deployment", False),
])
def test_supports_effort(model, expected):
    assert _supports_effort(model) is expected


# ---------------------------------------------------------------- documents
def test_store_roundtrip(tmp_path, sample_pdf):
    store = DocumentStore(tmp_path)
    meta = store.add("sample.pdf", sample_pdf)
    assert meta.page_count == 2 and meta.is_pdf
    doc = store.get(meta.id)
    assert "quick brown fox" in doc.native_text(0).text
    info0, info1 = doc.page_info(0), doc.page_info(1)
    assert not info0["looks_scanned"]
    assert info1["looks_scanned"] and info1["native_chars"] == 0
    assert doc.render(0, 50)[:8] == b"\x89PNG\r\n\x1a\n"
    assert doc.render(0, 300, "jpeg", max_edge_px=500)[:2] == b"\xff\xd8"
    assert [m.id for m in store.list()] == [meta.id]
    store.delete(meta.id)
    assert store.list() == []


def test_page_stats_cached_on_disk(tmp_path, sample_pdf):
    store = DocumentStore(tmp_path)
    meta = store.add("sample.pdf", sample_pdf)
    doc = store.get(meta.id)
    stats = doc.page_stats()
    cache = doc.folder / "pages.json"
    assert cache.exists() and len(stats) == 2 and not stats[0]["looks_scanned"] and stats[1]["looks_scanned"]

    # A fresh store (as after a restart) reads the file instead of re-extracting.
    fresh = DocumentStore(tmp_path).get(meta.id)
    fresh._compute_page_stats = None  # would raise if called
    assert fresh.page_stats() == stats

    # A damaged or missing file is derived data: it is rebuilt, not an error.
    for broken in ("not json", '{"version": 1, "pages": []}'):
        cache.write_text(broken)
        assert DocumentStore(tmp_path).get(meta.id).page_stats() == stats
    cache.unlink()
    assert DocumentStore(tmp_path).get(meta.id).page_stats() == stats


def test_page_infos_report_caches(tmp_path, sample_pdf):
    store = DocumentStore(tmp_path)
    doc = store.get(store.add("sample.pdf", sample_pdf).id)
    layer = TextLayer(source="ocr", text="x", words=[])
    doc.save_ocr_layer(1, "tesseract", layer)
    doc.save_ocr_layer(1, "easyocr", layer)
    doc.save_llm_result(0, "k1", {"text": "a"})
    doc.save_llm_result(0, "k2", {"text": "b"})
    infos = doc.page_infos()
    assert [i["number"] for i in infos] == [0, 1]
    assert infos[0]["ocr_engines"] == [] and not infos[0]["has_ocr_cache"] and infos[0]["llm_results"] == 2
    assert infos[1]["ocr_engines"] == ["tesseract", "easyocr"] and infos[1]["has_ocr_cache"]
    assert infos[1]["ocr_engines"] == doc.ocr_engines_cached(1) and infos[1]["llm_results"] == 0
    assert doc.page_info(1) == infos[1]
    with pytest.raises(DocumentError):
        doc.page_info(2)


def test_store_rejects_bad_input(tmp_path):
    store = DocumentStore(tmp_path)
    with pytest.raises(DocumentError):
        store.add("x.exe", b"MZ")
    with pytest.raises(DocumentError):
        store.add("x.pdf", b"not a pdf")
    with pytest.raises(DocumentError):
        store.get("../../etc")


def test_image_upload(tmp_path, sample_png):
    store = DocumentStore(tmp_path)
    meta = store.add("photo.png", sample_png)
    assert meta.page_count == 1 and not meta.is_pdf
    assert store.get(meta.id).page_info(0)["looks_scanned"]


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_ocr_scanned_page_and_cache(tmp_path, sample_pdf):
    store = DocumentStore(tmp_path)
    doc = store.get(store.add("sample.pdf", sample_pdf).id)
    layer = doc.ocr_text(1, dpi=300, language="eng")
    assert "quick brown fox" in layer.text.lower()
    assert search.find_boxes(layer.words, "brown")
    assert doc.cached_ocr_text(1).text == layer.text
