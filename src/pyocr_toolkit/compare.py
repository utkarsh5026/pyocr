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

Why a single error rate is not enough
-------------------------------------
CER counts edits but says nothing about *which* edits. A transcription that
drops every umlaut, one that misreads the total of an invoice, and one that
invents a sentence the page never had can all land on the same CER. So on
top of the rates this module also reports:

* :func:`error_breakdown`: word errors split into *misread* (substituted),
  *missed* (deleted) and *added* (inserted), each sorted into a kind
  (case, punctuation, accents, spacing, number, word) plus the most common
  character confusions (``0 -> O``, ``m -> rn``...).
* :func:`check_numbers`: every token that contains a digit is checked on its
  own, because one wrong digit in an amount or an ID matters more than ten
  wrong commas.
* :func:`cer_ladder`: CER recomputed as more kinds of difference are
  forgiven (case, punctuation, accents, added text). What is left at the end
  is real misreading; the steps show where the rest came from.

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
    ignore_accents: bool = False


def strip_accents(text: str) -> str:
    """``Müller`` -> ``Muller``, ``ß`` -> ``ss``: what an engine without the right language pack writes."""
    text = text.replace("ß", "ss").replace("ẞ", "SS")
    decomposed = unicodedata.normalize("NFKD", text)
    return unicodedata.normalize("NFC", "".join(ch for ch in decomposed if not unicodedata.combining(ch)))


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
    if opts.ignore_accents:
        text = strip_accents(text)
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
    if opts.ignore_accents:
        token = strip_accents(token)
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


# ------------------------------------------------------------ error analysis
_DIGIT_RE = re.compile(r"\d")
_EDGE_PUNCT_RE = re.compile(r"^[^\w]+|[^\w]+$")

#: Kinds of word error, in the order the UI lists them.
ERROR_KINDS = ("number", "word", "accents", "case", "punctuation", "spacing", "missed", "added", "noise")
_HAS_ALNUM_RE = re.compile(r"[^\W_]")


def _blocks(ref, hyp):
    """Yield runs of adjacent non-equal opcodes as ``(ops, ref_slice, hyp_slice)``.

    A word split in two ("recog nition") comes out of the alignment as a
    replace next to an insert; grouping neighbours lets us see it as one
    spacing error instead of a misread plus an added word.
    """
    run: list = []
    for op in Levenshtein.opcodes(ref, hyp):
        if op.tag != "equal":
            run.append(op)
            continue
        if run:
            yield run, ref[run[0].src_start:run[-1].src_end], hyp[run[0].dest_start:run[-1].dest_end]
            run = []
    if run:
        yield run, ref[run[0].src_start:run[-1].src_end], hyp[run[0].dest_start:run[-1].dest_end]


def classify_substitution(ref: str, hyp: str) -> str:
    """Name the kind of mistake that turns reference word ``ref`` into ``hyp``."""
    # lower(), not casefold(): casefold maps ß to "ss", which is an accent difference, not case.
    if ref.lower() == hyp.lower():
        return "case"
    # Before punctuation: "2.530,54" for "2,530.54" only moves punctuation, but it is a wrong number.
    if _DIGIT_RE.search(ref) or _DIGIT_RE.search(hyp):
        return "number"
    if ref.translate(_PUNCT_TABLE) == hyp.translate(_PUNCT_TABLE):
        return "punctuation"
    if strip_accents(ref).lower() == strip_accents(hyp).lower():
        return "accents"
    return "word"


_MAX_BLOCK_CELLS = 10_000


def _align_block_idx(ref: list[str], hyp: list[str]) -> list[tuple[str, int, int]]:
    """Re-align one run of differing words as ``(tag, ref_index, hyp_index)`` steps (-1 = none).

    Plain Levenshtein only counts edits, so when every word in a run differs
    many alignments tie and it may pair "INVOICE" with "Muller". This one still
    uses the fewest edits (so the counts add up to the word edit distance) but,
    among those, keeps exact matches and pairs words that look alike: a
    substitution weighs ``1 + dissimilarity``, an insertion or deletion ``1``. Very large runs (two unrelated
    texts) fall back to pairing by position to stay fast.
    """
    n, m = len(ref), len(hyp)
    if n * m > _MAX_BLOCK_CELLS:
        k = min(n, m)
        return ([("replace", i, i) for i in range(k)] + [("delete", i, -1) for i in range(k, n)]
                + [("insert", -1, j) for j in range(k, m)])
    inf = (float("inf"), float("inf"))
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    cost[0] = [(j, float(j)) for j in range(m + 1)]
    for i in range(1, n + 1):
        cost[i][0] = (i, float(i))
        for j in range(1, m + 1):
            r, h = ref[i - 1], hyp[j - 1]
            e, w = cost[i - 1][j - 1]
            diag = (e, w) if r == h else (e + 1, w + 1 + Levenshtein.normalized_distance(r, h))
            up, left = cost[i - 1][j], cost[i][j - 1]
            cost[i][j] = min(diag, (up[0] + 1, up[1] + 1), (left[0] + 1, left[1] + 1))
    steps: list[tuple[str, int, int]] = []
    i, j = n, m
    while i or j:
        if i and j:
            r, h = ref[i - 1], hyp[j - 1]
            e, w = cost[i - 1][j - 1]
            diag = (e, w) if r == h else (e + 1, w + 1 + Levenshtein.normalized_distance(r, h))
            if cost[i][j] == diag:
                steps.append(("equal" if r == h else "replace", i - 1, j - 1))
                i, j = i - 1, j - 1
                continue
        if i and cost[i][j] == (cost[i - 1][j][0] + 1, cost[i - 1][j][1] + 1):
            steps.append(("delete", i - 1, -1))
            i -= 1
        else:
            steps.append(("insert", -1, j - 1))
            j -= 1
    return steps[::-1]


def _align_block(ref: list[str], hyp: list[str]) -> list[tuple[str, str, str]]:
    """:func:`_align_block_idx` with the words themselves (``""`` for the missing side)."""
    return [(tag, ref[i] if i >= 0 else "", hyp[j] if j >= 0 else "") for tag, i, j in _align_block_idx(ref, hyp)]


def _merge_spacing(steps: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Fold a word split or joined across a space into one ``spacing`` step.

    "recognition" read as "recog nition" aligns as a replace next to an
    insert (a join, as a replace next to a delete). If the words on each
    side concatenate to the same text, it is one spacing error.
    """
    out: list[tuple[str, str, str]] = []
    k = 0
    while k < len(steps):
        merged = False
        if steps[k][0] == "replace":
            for extra in ("insert", "delete"):
                side = 2 if extra == "insert" else 1
                for direction in (1, -1):  # extra words after or before the replaced one
                    j, parts = k, [steps[k][side]]
                    while 0 <= j + direction < len(steps) and steps[j + direction][0] == extra:
                        j += direction
                        parts.append(steps[j][side])
                        joined = "".join(parts if direction == 1 else parts[::-1])
                        other = steps[k][3 - side]
                        if joined == other:
                            lo, hi = sorted((k, j))
                            if direction == -1:
                                out = out[: len(out) - (k - lo)]
                            words = steps[lo:hi + 1]
                            out.append(("spacing", " ".join(w[1] for w in words if w[1]),
                                        " ".join(w[2] for w in words if w[2])))
                            k = hi + 1
                            merged = True
                            break
                    if merged:
                        break
                if merged:
                    break
        if not merged:
            out.append(steps[k])
            k += 1
    return out


def error_breakdown(reference: str, hypothesis: str, opts: NormalizeOptions | None = None,
                    max_examples: int = 200, max_confusions: int = 12) -> dict:
    """Split the word errors into misread / missed / added, and sort them by kind.

    ``misread + missed + added`` equals ``score()["word_distance"]`` for the
    same options: they are the substitutions, deletions and insertions of a
    minimal word alignment. ``kinds`` counts the same errors by what went
    wrong, except that a word split or joined across a space counts once as
    ``spacing``, and added or missed tokens with no letter or digit in them
    (``|``, ``.``, ``_``: specks on a scan) count as ``noise``, so they don't
    look like invented text. ``confusions`` lists character substitutions inside misread
    words, most frequent first: an engine's fingerprint.
    """
    opts = opts or NormalizeOptions()
    ref, hyp = normalize(reference, opts).split(), normalize(hypothesis, opts).split()
    counts = {"misread": 0, "missed": 0, "added": 0}
    kinds = dict.fromkeys(ERROR_KINDS, 0)
    examples: list[dict] = []
    confusions: dict[tuple[str, str], int] = {}

    def note(kind: str, r: str, h: str, n: int = 1) -> None:
        kinds[kind] += n
        if len(examples) < max_examples:
            examples.append({"kind": kind, "ref": r, "hyp": h})

    for _, rw, hw in _blocks(ref, hyp):
        steps = _align_block(rw, hw)
        for tag, _, _ in steps:
            if tag != "equal":
                counts[{"replace": "misread", "delete": "missed", "insert": "added"}[tag]] += 1
        steps = _merge_spacing(steps)
        run: list[str] = []  # consecutive missed or added words become one example
        for k, (tag, r, h) in enumerate(steps):
            if tag in ("delete", "insert"):
                run.append(r or h)
                if k + 1 == len(steps) or steps[k + 1][0] != tag:
                    words = [w for w in run if _HAS_ALNUM_RE.search(w)]
                    if len(words) < len(run):
                        marks = " ".join(w for w in run if not _HAS_ALNUM_RE.search(w))
                        note("noise", marks if tag == "delete" else "", "" if tag == "delete" else marks,
                             len(run) - len(words))
                    if words:
                        text = " ".join(words)
                        note("missed", text, "", len(words)) if tag == "delete" else note("added", "", text, len(words))
                    run = []
            elif tag == "spacing":
                note("spacing", r, h)
            elif tag == "replace":
                note(classify_substitution(r, h), r, h)
                # neighbouring character edits form one confusion: "m" -> "rn", "ß" -> "fs"
                for _, rc, hc in _blocks(r, h):
                    confusions[(rc, hc)] = confusions.get((rc, hc), 0) + 1
    top = sorted(confusions.items(), key=lambda kv: (-kv[1], kv[0]))[:max_confusions]
    return {
        **counts,
        "kinds": {k: v for k, v in kinds.items() if v},
        "examples": examples,
        "confusions": [{"ref": r, "hyp": h, "count": n} for (r, h), n in top],
    }


def _number_key(token: str) -> str:
    return _EDGE_PUNCT_RE.sub("", token)


def check_numbers(reference: str, hypothesis: str, max_wrong: int = 50) -> dict:
    """Check every reference token that contains a digit: amounts, dates, IDs, phone numbers.

    Ignore-case and ignore-punctuation are deliberately not applied: with
    punctuation ignored, ``2.530,54`` and ``2,530.54`` would look equal, and for
    numbers that difference is the whole point. Only punctuation at the edges of
    a token (``(12)``, ``00.``, ``€386.50``) is dropped. ``wrong`` pairs each
    misread number with what the candidate wrote (``""`` = missing). ``extra``
    counts numbers that appear only in the candidate.
    """
    ref = [_number_key(t) for t in normalize(reference).split()]
    hyp = [_number_key(t) for t in normalize(hypothesis).split()]
    has_digit = [bool(_DIGIT_RE.search(t)) for t in ref]
    total = sum(has_digit)
    correct = sum(has_digit[i] for op in Levenshtein.opcodes(ref, hyp) if op.tag == "equal"
                  for i in range(op.src_start, op.src_end))
    extra = 0
    wrong: list[dict] = []
    for _, rw, hw in _blocks(ref, hyp):
        for tag, r, h in _align_block(rw, hw):
            if tag == "insert":
                extra += bool(_DIGIT_RE.search(h))
            elif tag == "equal":
                correct += bool(_DIGIT_RE.search(r))
            elif _DIGIT_RE.search(r) and len(wrong) < max_wrong:
                wrong.append({"ref": r, "hyp": h})
    return {"total": total, "correct": correct, "wrong": wrong, "extra": extra}


def _drop_added(reference: str, hypothesis: str) -> str:
    ref, hyp = reference.split(), hypothesis.split()
    keep = [w for op in Levenshtein.opcodes(ref, hyp) if op.tag != "insert" for w in hyp[op.dest_start:op.dest_end]]
    return " ".join(keep)


def cer_ladder(reference: str, hypothesis: str) -> list[dict]:
    """CER as more and more kinds of difference are forgiven, each step on top of the previous one.

    Exact -> ignore case -> ignore punctuation -> ignore accents -> drop added
    text. A big drop at a step tells you what most of the "errors" were; the
    last value is what is left when only real misreading and missing text count.
    A step never goes up, because each one forgives more than the last. Two
    things could otherwise make it rise: stripping punctuation shortens the
    reference, the denominator of CER; and words the word alignment calls
    "added" can be the closest character-level match for a misread word, so
    dropping them does not help.
    """
    steps = [("exact", "Exact", NormalizeOptions()),
             ("case", "Ignore case", NormalizeOptions(ignore_case=True)),
             ("punctuation", "Ignore punctuation", NormalizeOptions(True, True)),
             ("accents", "Ignore accents", NormalizeOptions(True, True, True))]
    out: list[dict] = []
    for k, label, o in steps:
        cer = score(reference, hypothesis, o)["cer"]
        out.append({"step": k, "label": label, "cer": min(cer, out[-1]["cer"]) if out else cer})
    folded = steps[-1][2]
    ref, hyp = normalize(reference, folded), normalize(hypothesis, folded)
    dropped = score(ref, _drop_added(ref, hyp))["cer"]
    out.append({"step": "added", "label": "Drop added text", "cer": min(dropped, out[-1]["cer"])})
    return out


# ---------------------------------------------------------------- consensus
def _words_with_breaks(text: str) -> tuple[list[str], dict[int, int]]:
    """Words as :func:`normalize` would split them, plus where lines end.

    ``breaks[i]`` is 1 when word ``i`` ends a line and 2 when it ends a
    paragraph, so a consensus can be laid out like the page it came from.
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    words: list[str] = []
    breaks: dict[int, int] = {}
    blank = False
    for line in text.splitlines():
        tokens = line.split()
        if not tokens:
            blank = True
            continue
        if blank and words:
            breaks[len(words) - 1] = 2
        blank = False
        words.extend(tokens)
        breaks[len(words) - 1] = 1
    if words:
        breaks.pop(len(words) - 1, None)
    return words, breaks


def consensus(texts: dict[str, str], opts: NormalizeOptions | None = None) -> dict:
    """Vote several transcriptions of one page into a consensus, word by word.

    No source is assumed to be right. Different engines make different
    mistakes (Tesseract reads 0 as O, an LLM "fixes" spelling), so where most
    of them agree on a word, that word is very likely what the page says.

    1. **Spine**: the source with the smallest total word distance to all the
       others; its words give the slots.
    2. **Align** every other source to the spine (the diff's word alignment,
       refined so similar-looking words pair up). Words a source has that the
       spine doesn't open extra slots after the spine word they follow.
    3. **Vote** per slot. Words are compared with ``opts`` (so with
       ignore-case, ``Invoice`` and ``INVOICE`` are the same vote); an empty
       cell is a vote for "no word here". Ties go to the spine.

    Returns the consensus ``text``, the aligned ``columns`` (each with every
    source's token and its word index in that source (``pos``, -1 = none;
    see :func:`split_words`), the ``winner``, its ``votes``, the ``dissent`` (indexes of
    sources that voted otherwise) and a ``status``: ``agree``
    = all sources, ``majority`` = more than half, ``tie`` = no majority, a
    close call for a person), ``counts`` of each status, and per source its
    ``score``, ``errors`` and ``numbers`` against the consensus, plus a
    ``matrix`` of every source scored against every other (row = reference).
    """
    opts = opts or NormalizeOptions()
    ids = list(texts)
    if len(ids) < 2:
        raise ValueError("a consensus needs at least two sources")
    parsed = {sid: _words_with_breaks(texts[sid]) for sid in ids}
    words = {sid: parsed[sid][0] for sid in ids}
    keys = {sid: [_key(w, opts) for w in words[sid]] for sid in ids}

    def total_distance(sid: str) -> int:
        return sum(Levenshtein.distance(keys[sid], keys[o]) for o in ids if o != sid)

    pivot = min(ids, key=total_distance)
    pw, pk = words[pivot], keys[pivot]
    n = len(pw)
    # per source: the word index (-1 = none) at each spine slot, and words inserted after a slot
    at: dict[str, list[int]] = {}
    ins: dict[str, dict[int, list[int]]] = {}
    for sid in ids:
        if sid == pivot:
            at[sid], ins[sid] = list(range(n)), {}
            continue
        sk = keys[sid]
        cells, extra = [-1] * n, {}
        for op in Levenshtein.opcodes(pk, sk):
            if op.tag == "equal":
                for k in range(op.src_end - op.src_start):
                    cells[op.src_start + k] = op.dest_start + k
        for ops, rk, hk in _blocks(pk, sk):
            a0, b0 = ops[0].src_start, ops[0].dest_start
            last = a0 - 1  # spine slot that inserted words follow
            for tag, i, j in _align_block_idx(rk, hk):
                if tag == "insert":
                    extra.setdefault(last, []).append(b0 + j)
                    continue
                last = a0 + i
                cells[a0 + i] = b0 + j if tag != "delete" else -1
        at[sid], ins[sid] = cells, extra

    def word(sid: str, idx: int) -> str:
        return words[sid][idx] if idx >= 0 else ""

    def column(pos: list[int], spine_token: str, br: int) -> dict:
        tokens = [word(sid, i) for sid, i in zip(ids, pos)]
        tally: dict[str, list[int]] = {}
        for idx, tok in enumerate(tokens):
            tally.setdefault(_key(tok, opts) if tok else "", []).append(idx)
        best = max(len(v) for v in tally.values())
        top = [k for k, v in tally.items() if len(v) == best]
        spine_key = _key(spine_token, opts) if spine_token else ""
        win_key = spine_key if spine_key in top else top[0]
        voters = tally[win_key]
        pidx = ids.index(pivot)
        winner = tokens[pidx] if pidx in voters else tokens[voters[0]]
        status = "agree" if best == len(ids) else "majority" if best * 2 > len(ids) else "tie"
        dissent = [i for i in range(len(tokens)) if i not in voters]
        return {"tokens": tokens, "pos": pos, "winner": winner, "votes": best, "status": status, "dissent": dissent,
                "br": br}

    breaks = parsed[pivot][1]
    columns: list[dict] = []
    for i in range(-1, n):
        group: list[dict] = []
        if i >= 0:
            group.append(column([at[sid][i] for sid in ids], pw[i], 0))
        width = max((len(ins[sid].get(i, [])) for sid in ids), default=0)
        for k in range(width):
            pos = [(ins[sid].get(i, [])[k] if k < len(ins[sid].get(i, [])) else -1) for sid in ids]
            group.append(column(pos, "", 0))
        if group and i >= 0:
            group[-1]["br"] = breaks.get(i, 0)
        columns.extend(group)

    parts: list[str] = []
    for col in columns:
        if col["winner"]:
            parts.append(col["winner"])
            parts.append({0: " ", 1: "\n", 2: "\n\n"}[col["br"]])
        elif col["br"] and parts:
            parts[-1] = {1: "\n", 2: "\n\n"}[col["br"]]
    text = "".join(parts).strip()

    counts = {"words": sum(bool(c["winner"]) for c in columns), "agree": 0, "majority": 0, "tie": 0}
    for c in columns:
        counts[c["status"]] += 1
    per_source = {}
    for sid in ids:
        sc = score(text, texts[sid], opts)
        br = error_breakdown(text, texts[sid], opts, max_examples=0, max_confusions=0)
        num = check_numbers(text, texts[sid], max_wrong=10)
        per_source[sid] = {"score": sc, "errors": {k: br[k] for k in ("misread", "missed", "added")},
                           "kinds": br["kinds"], "numbers": num}
    matrix = {a: {b: (None if a == b else {k: v for k, v in score(texts[a], texts[b], opts).items()
                                           if k in ("cer", "wer", "char_distance", "reference_chars")})
                  for b in ids} for a in ids}
    return {"sources": ids, "pivot": pivot, "text": text, "columns": columns, "counts": counts,
            "per_source": per_source, "matrix": matrix}


def split_words(text: str) -> list[str]:
    """The words of ``text`` as :func:`consensus` numbers them (its columns' ``pos``)."""
    return _words_with_breaks(text)[0]


def map_word_boxes(text: str, boxed_words: list[tuple[str, list[float]]]) -> list[list[float] | None]:
    """A box for each word of ``text`` (as :func:`split_words` splits it), or ``None``.

    ``boxed_words`` is the engine's own word list with its boxes (native text or
    OCR). The two lists mostly match one to one, but not always: a word
    hyphenated across a line is one word in the text and two boxes. Aligning
    them keeps the boxes right after such a spot; a word joined from two boxes
    gets their union.
    """
    tokens = split_words(text)
    keys = [unicodedata.normalize("NFKC", w) for w, _ in boxed_words]
    boxes: list[list[float] | None] = [None] * len(tokens)
    for op in Levenshtein.opcodes(tokens, keys):
        if op.tag == "equal":
            for k in range(op.src_end - op.src_start):
                boxes[op.src_start + k] = boxed_words[op.dest_start + k][1]
    for ops, rt, rk in _blocks(tokens, keys):
        a0, b0 = ops[0].src_start, ops[0].dest_start
        if not rt or not rk:
            continue
        # pair by position; the last word takes the union of any boxes left over (a word joined across a line)
        for k in range(min(len(rt), len(rk))):
            last = len(rk) if k == len(rt) - 1 else k + 1
            rects = [boxed_words[b0 + m][1] for m in range(k, last)]
            boxes[a0 + k] = [min(r[0] for r in rects), min(r[1] for r in rects),
                             max(r[2] for r in rects), max(r[3] for r in rects)]
    return boxes
