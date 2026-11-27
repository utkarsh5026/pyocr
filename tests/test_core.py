from __future__ import annotations

import pytest

from pyocr_toolkit import compare, search
from pyocr_toolkit.documents import DocumentError, DocumentStore, Word
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


INVOICE_REF = "Bill to Müller & Söhne, Köln. Total due €2,530.54 within 30 days."


def test_error_breakdown_splits_misread_missed_added():
    ref = "the quick brown fox jumps over the dog"
    hyp = "the quack brown jumps over the lazy dog today"
    b = compare.error_breakdown(ref, hyp)
    assert (b["misread"], b["missed"], b["added"]) == (1, 1, 2)
    # the split is exactly the word edit distance, so it explains the WER
    assert b["misread"] + b["missed"] + b["added"] == compare.score(ref, hyp)["word_distance"]
    assert b["kinds"] == {"word": 1, "missed": 1, "added": 2}
    assert {"kind": "missed", "ref": "fox", "hyp": ""} in b["examples"]


def test_error_breakdown_kinds():
    ref = "INVOICE Müller Straße 2024 done. recognition"
    hyp = "Invoice Muller Strasse 2O24 done! recog nition"
    kinds = compare.error_breakdown(ref, hyp)["kinds"]
    # ß -> ss is an accent difference, not case (casefold() would call it case)
    assert kinds == {"case": 1, "accents": 2, "number": 1, "punctuation": 1, "spacing": 1}
    # with ignore_case on, case differences are not errors at all
    assert "case" not in compare.error_breakdown(ref, hyp, compare.NormalizeOptions(ignore_case=True))["kinds"]


def test_error_breakdown_stray_marks_are_noise_not_added_text():
    b = compare.error_breakdown("Readings at noon", "Readings | at . noon _ extra")
    assert b["added"] == 4  # still counted as added words in the split...
    assert b["kinds"] == {"noise": 3, "added": 1}  # ...but scan specks are their own kind


def test_cer_ladder_never_goes_up():
    for hyp in ("12:00°> iInch. : 7 13220 Found", "Lunch | . noon", "totally different words here"):
        cers = [s["cer"] for s in compare.cer_ladder("12:00 Lunch. 13:20 Found", hyp)]
        assert cers == sorted(cers, reverse=True)


def test_error_breakdown_confusions_merge_neighbouring_chars():
    conf = compare.error_breakdown("Payment 2024 10 30", "Payrnent 2O24 1O 3O")["confusions"]
    assert conf[0] == {"ref": "0", "hyp": "O", "count": 3}
    assert {"ref": "m", "hyp": "rn", "count": 1} in conf


def test_check_numbers():
    hyp = "Bill to Muller & Sohne, Koln. Total due €2.530,54 within 3O days."
    n = compare.check_numbers(INVOICE_REF, hyp)
    assert (n["total"], n["correct"]) == (2, 0)
    # swapped separators are a wrong number even though only punctuation moved
    assert n["wrong"] == [{"ref": "2,530.54", "hyp": "2.530,54"}, {"ref": "30", "hyp": "3O"}]
    # edge punctuation and currency symbols don't count
    assert compare.check_numbers("(12) €5.00.", "12 5.00")["correct"] == 2
    n = compare.check_numbers("pay within 30 days", "pay within days, call 555")
    assert n["wrong"] == [{"ref": "30", "hyp": ""}] and n["extra"] == 1


def test_cer_ladder_explains_the_gap():
    styled = "BILL TO Muller & Sohne Koln Total due €2,530.54 within 30 days!"
    ladder = compare.cer_ladder(INVOICE_REF, styled)
    assert [s["step"] for s in ladder] == ["exact", "case", "punctuation", "accents", "added"]
    cers = [s["cer"] for s in ladder]
    assert cers[0] > cers[1] > cers[2] > 0 and cers[3] == 0  # only style: nothing left
    invented = INVOICE_REF + " Please call us with any questions."
    ladder = compare.cer_ladder(INVOICE_REF, invented)
    assert ladder[0]["cer"] > 0.3 and ladder[-1]["cer"] == 0  # all of it was added text


def test_consensus_votes_word_by_word():
    texts = {
        "ocr": "INVOICE 2O24\nBill to Muller GmbH",
        "a": "INVOICE 2024\nBill to Müller GmbH",
        "b": "Invoice 2024\nBill to Müller GmbH",
        "c": "INVOICE 2024\nBill to Müller GmbH. Call us anytime!",
    }
    res = compare.consensus(texts, compare.NormalizeOptions())
    assert res["text"] == "INVOICE 2024\nBill to Müller GmbH"  # each odd one out loses, lines kept
    assert res["counts"] == {"words": 6, "agree": 2, "majority": 7, "tie": 0}
    # words only "c" has get slots of their own, which the other three vote empty
    added = [c for c in res["columns"] if not c["winner"]]
    assert [c["tokens"] for c in added] == [["", "", "", w] for w in ("Call", "us", "anytime!")]
    assert res["per_source"]["c"]["errors"]["added"] == 3
    assert res["per_source"]["ocr"]["numbers"]["correct"] == 0
    # with ignore-case, "Invoice" and "INVOICE" are the same vote
    res = compare.consensus(texts, compare.NormalizeOptions(ignore_case=True))
    assert res["columns"][0]["status"] == "agree"


def test_consensus_tie_goes_to_the_spine():
    res = compare.consensus({"a": "Rheinstraße 12", "b": "Rheinstrasse 12", "c": "Rheinstraße 12",
                             "d": "Rheinstrafse 12"})
    first = res["columns"][0]
    assert first["status"] == "tie" and first["votes"] == 2 and first["winner"] == "Rheinstraße"
    assert res["counts"]["tie"] == 1


def test_consensus_needs_two_sources():
    with pytest.raises(ValueError):
        compare.consensus({"a": "x"})


def test_consensus_columns_point_back_to_each_sources_words():
    texts = {"a": "one two three", "b": "one too three four"}
    res = compare.consensus(texts)
    for col in res["columns"]:
        for sid, i, tok in zip(res["sources"], col["pos"], col["tokens"]):
            assert (compare.split_words(texts[sid])[i] if i >= 0 else "") == tok


def test_map_word_boxes_follows_the_text():
    boxes = [("a", [0, 0, 1, 1]), ("recog-", [2, 0, 5, 1]), ("nition", [0, 2, 4, 3]), ("b", [5, 2, 6, 3])]
    # the text joins the hyphenated word; it gets the union of both boxes, and "b" keeps its own
    assert compare.map_word_boxes("a recog-\nnition b", boxes) == [[0, 0, 1, 1], [0, 0, 5, 3], [5, 2, 6, 3]]
    assert compare.map_word_boxes("a extra b", [("a", [0, 0, 1, 1]), ("b", [5, 2, 6, 3])]) == \
        [[0, 0, 1, 1], None, [5, 2, 6, 3]]


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
