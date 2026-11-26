"""Prompts for page transcription.

The goal of the default ("verbatim") prompt is to make the LLM behave like a
very good OCR engine, *not* like an assistant: no fixing typos, no
summarising, no commentary. That makes its output a fair yardstick for the
Tesseract / native text layer, and shows what an LLM pipeline would actually
receive.

Batched requests (several pages per call, as many production pipelines do)
reuse the same prompt and append ``batch_rules``: one marker line per page so
the reply can be split back into pages. The rules name the batch size, so
"5 pages per request" hashes to its own cache key and shows up as its own
source next to the single-page one. The single-page prompts are unchanged, so
existing cache keys stay valid.
"""

from __future__ import annotations

import re

_COMMON_RULES = """\
Rules:
- Transcribe ALL visible text: headers, footers, page numbers, captions, footnotes, stamps, \
watermarks, form labels and handwriting.
- Copy the text exactly as printed. Do NOT correct spelling, grammar, capitalisation or \
punctuation, do NOT translate, summarise, reorder or add anything.
- Keep numbers, dates, currency symbols, codes and IDs character-for-character.
- Follow natural reading order. For multi-column layouts, finish a column top to bottom \
before starting the next one.
- If a word is truly illegible write [illegible]; if a character is uncertain, give your \
best reading without comment.
- Output only the transcription: no preamble, no explanation, no code fences."""

VERBATIM_SYSTEM = f"""\
You are a meticulous transcription engine used to audit OCR quality. You receive an image \
of one document page and return its text exactly as it appears.

{_COMMON_RULES}
- Plain text only. Start a new line wherever the printed line ends. Keep end-of-line \
hyphenation as printed.
- For tables write one row per line with cells separated by " | "."""

MARKDOWN_SYSTEM = f"""\
You are a meticulous document transcription engine. You receive an image of one document \
page and return its full text as Markdown that preserves the structure.

{_COMMON_RULES}
- Use Markdown headings for headings, lists for lists, and GitHub-flavoured Markdown tables \
for tables (keep every cell, including empty ones).
- Join lines that belong to the same paragraph; separate paragraphs with a blank line.
- Describe non-text content only briefly in brackets, e.g. [figure: bar chart], [signature], \
[logo]."""

USER_PROMPT = "Transcribe this page."
BATCH_USER_PROMPT = "Transcribe each of these pages."

#: most pages one request may carry; providers cap images per request and output length
MAX_PAGES_PER_REQUEST = 20

PROMPT_MODES = {
    "verbatim": {"label": "Verbatim (plain text, line by line)", "system": VERBATIM_SYSTEM},
    "markdown": {"label": "Markdown (keep structure & tables)", "system": MARKDOWN_SYSTEM},
}


def resolve_prompt(mode: str, custom_prompt: str | None = None) -> str:
    if mode == "custom":
        if not custom_prompt or not custom_prompt.strip():
            raise ValueError("custom prompt mode needs a prompt")
        return custom_prompt.strip()
    try:
        return PROMPT_MODES[mode]["system"]
    except KeyError as exc:
        raise ValueError(f"unknown prompt mode '{mode}'") from exc


def batch_rules(pages_per_request: int) -> str:
    """Instructions appended to the system prompt for a request with several pages.

    Deliberately independent of which pages a request holds (or of a short last
    chunk), so every page of a run shares one cache key.
    """
    return f"""

Batched pages:
- This message holds up to {pages_per_request} pages of the same document. Each image comes right after \
a label such as "Page 5". Everything above applies to every page.
- Transcribe the pages in the order given. Start each page with a marker line on its own, using the \
number from that page's label, exactly like this:
=== PAGE 5 ===
- Write the marker even for a blank page, and nothing else before the first marker.
- Keep every page separate: never join a word, sentence or table across a page break, and never move \
text from one page to another."""


_MARKER = re.compile(r"^[ \t#*_>`]*=+[ \t]*PAGE[ \t]+(\d+)[ \t]*=+[ \t*_`]*$", re.IGNORECASE | re.MULTILINE)


def split_batch(text: str, labels: list[int]) -> tuple[dict[int, str], list[str]]:
    """Split a batched reply into ``{label: text}`` using its ``=== PAGE n ===`` markers.

    ``labels`` are the 1-based page numbers that were sent, in order. Only pages whose
    marker appears exactly once are returned, so a reply that merges, skips or
    renumbers pages never puts one page's text under another. The second value lists
    what went wrong, for warnings. A one-page request without a marker is accepted
    whole: there is nothing to mix up.
    """
    found = list(_MARKER.finditer(text))
    problems: list[str] = []
    if not found:
        if len(labels) == 1:
            return {labels[0]: text.strip()}, []
        return {}, ["the reply had no page markers"]
    counts: dict[int, int] = {}
    for m in found:
        counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
    if text[:found[0].start()].strip():
        problems.append("text before the first page marker was dropped")
    unexpected = sorted(n for n in counts if n not in labels)
    if unexpected:
        problems.append("markers for pages that were not sent: " + ", ".join(map(str, unexpected)))
    out: dict[int, str] = {}
    for i, m in enumerate(found):
        n = int(m.group(1))
        if n not in labels or counts[n] != 1:
            continue
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        out[n] = text[m.end():end].strip()
    repeated = sorted(n for n, c in counts.items() if c > 1 and n in labels)
    if repeated:
        problems.append("pages marked more than once: " + ", ".join(map(str, repeated)))
    missing = [n for n in labels if n not in counts]
    if missing:
        problems.append("no marker for pages " + ", ".join(map(str, missing)))
    return out, problems
