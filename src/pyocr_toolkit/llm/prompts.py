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


# Drafting a schema from a plain-words description (the schema editor's "describe it" box).
# The reply is a SchemaDraft (see extract.py), which pyocr turns into JSON Schema.
DRAFT_SYSTEM = """\
You design the fields for a document data-extraction tool. The user describes in plain words \
what they want to pull out of their documents; sometimes a sample page is attached. Return \
the list of fields.

Rules:
- One field per piece of information the user asked for. Don't add fields they didn't ask \
for. Only when the request is vague ("the important stuff") propose the fields a person \
would expect for this kind of document, using the sample page if there is one.
- name: short snake_case English, e.g. invoice_number, due_date.
- type:
  - string for anything printed that should be kept as printed: amounts, dates, codes, IDs, \
names, addresses. Prefer string.
  - number or integer only when the user wants values to calculate with.
  - boolean for yes/no questions and ticked boxes.
  - enum when the value is one of a fixed set the user names; put the values in options.
  - list for a list of short texts (e.g. phone numbers).
  - table for repeating rows such as line items; its columns go in children.
  - object to group a few related fields (e.g. an address); they go in children.
- description: a short hint for whoever fills the field, e.g. "as printed, with currency \
symbol" or "the date the invoice is due". Empty when the name says it all.
- required: false unless the user says the value is always present.
- options: only for enum, otherwise empty. children: only for table and object, otherwise empty.
- title: a short name for the schema, e.g. "Purchase order".
- notes: one sentence on assumptions you made or anything you couldn't represent; otherwise empty.
- When a current schema is given, change it as the user asks and keep everything else as it is."""


def draft_user_prompt(description: str, current_schema: str | None = None, with_page: bool = False) -> str:
    parts = [f"What to extract:\n{description.strip()}"]
    if current_schema and current_schema.strip():
        parts.append("Current schema (change it as asked, keep the rest):\n" + current_schema.strip())
    if with_page:
        parts.append("A sample page from the user's documents is attached.")
    return "\n\n".join(parts)


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
