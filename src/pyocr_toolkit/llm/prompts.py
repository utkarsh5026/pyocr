"""Prompts for page transcription.

The goal of the default ("verbatim") prompt is to make the LLM behave like a
very good OCR engine, *not* like an assistant: no fixing typos, no
summarising, no commentary. That makes its output a fair yardstick for the
Tesseract / native text layer, and shows what an LLM pipeline would actually
receive.
"""

from __future__ import annotations

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
