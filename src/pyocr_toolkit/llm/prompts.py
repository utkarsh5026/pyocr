"""Prompts for page transcription.

The goal of the default ("verbatim") prompt is to make the LLM behave like a
very good OCR engine, *not* like an assistant: no fixing typos, no
summarising, no commentary. That makes its output a fair yardstick for the
Tesseract / native text layer, and shows what an LLM pipeline would actually
receive.
"""

from __future__ import annotations

import json

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

# Structured extraction (prompt mode "extract"). Not in PROMPT_MODES: it needs a
# schema, so the UI offers it separately. Same spirit as the verbatim prompt:
# copy what is printed, never fill gaps.
EXTRACT_MODE = "extract"

EXTRACT_SYSTEM = """\
You are a meticulous document data-extraction engine. You receive an image of one document \
page and fill a JSON object whose shape is fixed by a JSON Schema.

Rules:
- Use only what is printed or handwritten on this page. Never guess, infer, compute or look \
up a value that isn't there.
- For string fields copy the text exactly as printed: keep currency symbols, separators, \
date formats, codes and capitalisation. Do NOT normalise, convert, translate or correct.
- For number, integer or boolean fields give the printed value in that type.
- Use null for a field that does not appear on the page, and an empty list when there are \
no rows for a list.
- Lists (line items, table rows, form fields) get one entry per printed row, in reading order.
- If a value is truly illegible write [illegible].
- Output only the JSON object: no preamble, no explanation, no code fences."""

EXTRACT_USER_PROMPT = "Extract the data from this page."


def schema_instructions(schema: dict) -> str:
    """Appended to the system prompt for models that can't enforce a schema natively."""
    return ("\n\nReturn exactly one JSON object that validates against this JSON Schema "
            "(every property present, null when not on the page):\n" + json.dumps(schema, ensure_ascii=False))

PROMPT_MODES = {
    "verbatim": {"label": "Verbatim (plain text, line by line)", "system": VERBATIM_SYSTEM},
    "markdown": {"label": "Markdown (keep structure & tables)", "system": MARKDOWN_SYSTEM},
}


def resolve_prompt(mode: str, custom_prompt: str | None = None) -> str:
    if mode == EXTRACT_MODE:
        return EXTRACT_SYSTEM
    if mode == "custom":
        if not custom_prompt or not custom_prompt.strip():
            raise ValueError("custom prompt mode needs a prompt")
        return custom_prompt.strip()
    try:
        return PROMPT_MODES[mode]["system"]
    except KeyError as exc:
        raise ValueError(f"unknown prompt mode '{mode}'") from exc
