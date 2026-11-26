"""Text comparison: error rates and a word-level diff.

How the numbers are defined
---------------------------
We compare a *hypothesis* (the text we are judging, e.g. Tesseract output)
against a *reference* (the text we trust more, e.g. an LLM transcription or
the PDF's own text layer).

* **Edit distance** (Levenshtein) is the minimum number of single-item
  insertions, deletions and substitutions that turn one sequence into the
  other.
* **CER** (character error rate) = char edit distance / reference length.
* **WER** (word error rate) = word edit distance / reference word count.

Both are 0.0 for a perfect match and can exceed 1.0 when the hypothesis has
lots of extra text. ``accuracy`` is ``max(0, 1 - CER)``, a friendlier number.

Whitespace is collapsed before scoring because OCR engines and LLMs disagree
wildly on line breaks and spacing, which says nothing about whether the
characters were *read* correctly. Optional flags also ignore case and
punctuation.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

_PUNCT_TABLE = str.maketrans("", "", string.punctuation + "“”‘’«»–—…•·")
_TOKEN_RE = re.compile(r"\n|[^\S\n]+|[^\s]+")


@dataclass
class NormalizeOptions:
    ignore_case: bool = False
    ignore_punctuation: bool = False


def normalize(text: str, opts: NormalizeOptions | None = None) -> str:
    opts = opts or NormalizeOptions()
    # NFKC folds ligatures (ﬁ -> fi) and full-width forms, which PDFs love.
    text = unicodedata.normalize("NFKC", text)
    # Undo hyphenation at line ends: "recog-\nnition" -> "recognition".
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    if opts.ignore_case:
        text = text.casefold()
    if opts.ignore_punctuation:
        text = text.translate(_PUNCT_TABLE)
    return " ".join(text.split())


def score(reference: str, hypothesis: str, opts: NormalizeOptions | None = None) -> dict:
    ref = normalize(reference, opts)
    hyp = normalize(hypothesis, opts)
    ref_words, hyp_words = ref.split(), hyp.split()
    char_dist = Levenshtein.distance(ref, hyp)
    word_dist = Levenshtein.distance(ref_words, hyp_words)
    cer = char_dist / len(ref) if ref else (0.0 if not hyp else 1.0)
    wer = word_dist / len(ref_words) if ref_words else (0.0 if not hyp_words else 1.0)
    return {
        "cer": round(cer, 4),
        "wer": round(wer, 4),
        "accuracy": round(max(0.0, 1.0 - cer), 4),
        "char_distance": char_dist,
        "word_distance": word_dist,
        "reference_chars": len(ref),
        "hypothesis_chars": len(hyp),
        "reference_words": len(ref_words),
        "hypothesis_words": len(hyp_words),
    }


def _tokens(text: str) -> list[str]:
    """Split into words, keeping newlines as their own tokens (spaces dropped)."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    return [t for t in _TOKEN_RE.findall(text) if t == "\n" or not t.isspace()]


def _key(token: str, opts: NormalizeOptions) -> str:
    if opts.ignore_case:
        token = token.casefold()
    if opts.ignore_punctuation:
        token = token.translate(_PUNCT_TABLE)
    return token


def word_diff(reference: str, hypothesis: str, opts: NormalizeOptions | None = None) -> list[dict]:
    """Word-level diff as a list of segments for display.

    Each segment is ``{"op": "equal"|"delete"|"insert"|"replace", "ref": str, "hyp": str}``.
    ``delete`` = present in the reference but missed by the hypothesis;
    ``insert`` = extra text in the hypothesis.
    Line breaks are ignored when aligning (so a reflowed paragraph still
    matches) but kept in the output text so the diff stays readable.
    """
    opts = opts or NormalizeOptions()
    ref_tokens, hyp_tokens = _tokens(reference), _tokens(hypothesis)
    ref_words = [i for i, t in enumerate(ref_tokens) if t != "\n"]
    hyp_words = [i for i, t in enumerate(hyp_tokens) if t != "\n"]
    ref_keys = [_key(ref_tokens[i], opts) for i in ref_words]
    hyp_keys = [_key(hyp_tokens[i], opts) for i in hyp_words]

    def join(tokens: list[str], idx: list[int], start: int, end: int) -> str:
        if start >= end:
            return ""
        # include newlines between the selected words and the ones just
        # before the first word, so line structure survives segment borders
        lo = idx[start - 1] + 1 if start > 0 else 0
        hi = idx[end - 1]
        out = []
        for tok in tokens[lo : hi + 1]:
            if tok == "\n":
                out.append("\n")
            else:
                if out and out[-1] != "\n":
                    out.append(" ")
                out.append(tok)
        return "".join(out)

    segments: list[dict] = []
    for op in Levenshtein.opcodes(ref_keys, hyp_keys):
        segments.append({
            "op": op.tag,
            "ref": join(ref_tokens, ref_words, op.src_start, op.src_end),
            "hyp": join(hyp_tokens, hyp_words, op.dest_start, op.dest_end),
        })
    # Merge neighbouring segments of the same kind to keep the payload small.
    merged: list[dict] = []
    for seg in segments:
        if merged and merged[-1]["op"] == seg["op"]:
            for side in ("ref", "hyp"):
                if seg[side]:
                    sep = "" if not merged[-1][side] or seg[side].startswith("\n") else " "
                    merged[-1][side] += sep + seg[side]
        else:
            merged.append(dict(seg))
    return merged
