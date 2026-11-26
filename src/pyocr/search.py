"""Search inside a page's text layers.

Two kinds of hits are produced:

* **Boxes** - for the native text layer and Tesseract OCR we know where every
  word sits on the page, so matches come back with rectangles (PDF points)
  that the UI draws over the page image.
* **Snippets** - for every source (including LLM transcriptions, which have
  no coordinates) we return the surrounding text with the match marked.

Matching is case-insensitive. A multi-word query matches a run of
consecutive words where the first query word may be a suffix of a page word,
the last may be a prefix, and the ones in the middle must match exactly -
so "quick bro" finds "The quick brown fox".
"""

from __future__ import annotations

import re
import unicodedata

from .documents import Word


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _strip_punct(token: str) -> str:
    return token.strip(".,;:!?\"'()[]{}<>“”‘’«»")


def find_boxes(words: list[Word], query: str) -> list[list[float]]:
    q = [_fold(t) for t in query.split()]
    if not q or not words:
        return []
    folded = [_fold(w.text) for w in words]
    hits: list[list[float]] = []
    if len(q) == 1:
        for w, f in zip(words, folded):
            if q[0] in f:
                hits.append(w.rect)
        return hits
    n = len(q)
    q = [_strip_punct(t) for t in q]
    stripped = [_strip_punct(f) for f in folded]
    for i in range(len(words) - n + 1):
        window = stripped[i : i + n]
        if (
            window[0].endswith(q[0])
            and window[-1].startswith(q[-1])
            and window[1:-1] == q[1:-1]
        ):
            hits.extend(w.rect for w in words[i : i + n])
    return hits


def find_snippets(text: str, query: str, context: int = 60, limit: int = 20) -> list[dict]:
    """Return up to ``limit`` snippets around case-insensitive matches.

    Whitespace in the query matches any run of whitespace in the text, so a
    phrase broken across lines is still found.
    """
    terms = query.split()
    if not terms:
        return []
    pattern = re.compile(r"\s+".join(re.escape(t) for t in terms), re.IGNORECASE)
    snippets = []
    for m in pattern.finditer(text):
        start, end = m.start(), m.end()
        before = text[max(0, start - context) : start]
        after = text[end : end + context]
        snippets.append({
            "before": ("…" if start > context else "") + before.replace("\n", " "),
            "match": m.group(0).replace("\n", " "),
            "after": after.replace("\n", " ") + ("…" if end + context < len(text) else ""),
            "offset": start,
        })
        if len(snippets) >= limit:
            break
    return snippets


def count_matches(text: str, query: str) -> int:
    terms = query.split()
    if not terms:
        return 0
    return len(re.findall(r"\s+".join(re.escape(t) for t in terms), text, re.IGNORECASE))
