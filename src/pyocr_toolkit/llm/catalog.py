"""A curated catalog of vision models, rated for *this* job: verbatim page transcription.

Why a hand-written catalog instead of only asking each provider for its model list?
Provider ``/models`` endpoints return dozens of ids (embeddings, TTS, image generators)
and say nothing about which ones read a scanned page well. The catalog adds that missing
judgement so the UI can say "good for OCR" / "not suitable" next to a name. It is a
suggestion list, never a whitelist: any model name the user types is still sent as-is.

Ratings were compiled in November 2026 from the providers' own model pages and public
OCR benchmarks (see ``SOURCES``). Benchmarks disagree and go stale quickly, so each entry
says *why* it got its rating, and ``evidence`` separates measured results from estimates.
Test on your own pages: the Compare tab exists exactly for that.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

#: rating -> (short label, sort order). "unrated" is only produced by :func:`lookup`.
RATINGS: dict[str, tuple[str, int]] = {
    "excellent": ("Excellent for OCR", 0),
    "good": ("Good for OCR", 1),
    "fair": ("Fair: clean print only", 2),
    "unsuitable": ("Not suitable", 4),
    "unrated": ("Not rated", 3),
}

SOURCES = [
    ("Roboflow: Best LLMs for OCR (Oct 2026)", "https://roboflow.com/blog/best-llms-for-ocr"),
    ("Roboflow OCR leaderboard (updated Nov 19, 2026)", "https://playground.roboflow.com/models/task/ocr"),
    ("Docsumo: OmniDocBench roundup", "https://www.docsumo.com/blog/best-ocr-models"),
    ("Anthropic models overview", "https://platform.claude.com/docs/en/about-claude/models/overview"),
    ("OpenAI models", "https://developers.openai.com/api/docs/models"),
    ("Gemini models", "https://ai.google.dev/gemini-api/docs/models"),
]
CATALOG_UPDATED = "2026-11"

# Provider ids a model *family* can be used with. Azure / Foundry / OpenAI-compatible take
# deployment or server-specific names, which :func:`lookup` matches loosely.
_CLAUDE = ("anthropic", "azure_claude", "openai_compatible")
_OPENAI = ("openai", "azure_openai", "openai_compatible")
_GEMINI = ("gemini", "openai_compatible")
_OPEN = ("openai_compatible",)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    vendor: str
    providers: tuple[str, ...]
    rating: str
    note: str
    #: "benchmark" = measured on a public OCR benchmark; "vendor" = vendor docs + tier estimate
    evidence: str = "vendor"
    #: current | previous | preview | deprecated
    status: str = "current"
    #: list price, USD per 1M input / output tokens (None = not published)
    price_in: float | None = None
    price_out: float | None = None
    #: the name differs per server (Ollama tag, OpenRouter slug, HF repo); ``id`` is the family
    name_varies: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict:
        d = asdict(self)
        d["providers"] = list(self.providers)
        d["aliases"] = list(self.aliases)
        d["rating_label"] = RATINGS[self.rating][0]
        return d


def _m(*args, **kw) -> ModelInfo:
    return ModelInfo(*args, **kw)


MODELS: list[ModelInfo] = [
    # ---------------------------------------------------------------- Anthropic
    _m("claude-fable-5-2", "Claude Fable 5.2", "Anthropic", _CLAUDE, "excellent",
       "Top accuracy pick. Its predecessor Fable 5 ranked #1 of 88 models (94%) on Roboflow's OCR benchmark; "
       "strongest on dense tables and charts. Also the slowest and most expensive.",
       evidence="benchmark", price_in=8, price_out=40),
    _m("claude-opus-5-5", "Claude Opus 5.5", "Anthropic", _CLAUDE, "excellent",
       "Strong vision and instruction following at half Fable's price; reliable at copying text without "
       "'fixing' it. Not separately benchmarked.", price_in=4, price_out=20),
    _m("claude-sonnet-5-5", "Claude Sonnet 5.5", "Anthropic", _CLAUDE, "good",
       "Balanced cost/quality for bulk jobs. Raise effort for handwriting.", price_in=2, price_out=10),
    _m("claude-haiku-5-5", "Claude Haiku 5.5", "Anthropic", _CLAUDE, "fair",
       "Very cheap and fast; fine for clean printed pages, weaker on handwriting and complex layouts.",
       price_in=0.1, price_out=0.5),
    _m("claude-fable-5-1", "Claude Fable 5.1", "Anthropic", _CLAUDE, "excellent",
       "Previous Fable; still top-tier, but 5.2 is cheaper.", status="previous", price_in=10, price_out=50),
    _m("claude-opus-5", "Claude Opus 5", "Anthropic", _CLAUDE, "good", "Previous Opus.", status="previous",
       price_in=5, price_out=25),
    _m("claude-sonnet-5", "Claude Sonnet 5", "Anthropic", _CLAUDE, "good", "Previous Sonnet.", status="previous",
       price_in=2, price_out=10),
    _m("claude-opus-4-8", "Claude Opus 4.8", "Anthropic", _CLAUDE, "good", "Older generation.", status="previous",
       price_in=5, price_out=25),
    _m("claude-opus-4-7", "Claude Opus 4.7", "Anthropic", _CLAUDE, "good", "Older generation.", status="previous",
       price_in=5, price_out=25),
    _m("claude-opus-4-6", "Claude Opus 4.6", "Anthropic", _CLAUDE, "good", "Older generation.", status="previous",
       price_in=5, price_out=25),
    _m("claude-sonnet-4-6", "Claude Sonnet 4.6", "Anthropic", _CLAUDE, "fair", "Older generation.",
       status="previous", price_in=3, price_out=15),
    _m("claude-haiku-4-5", "Claude Haiku 4.5", "Anthropic", _CLAUDE, "fair",
       "Older and pricier than Haiku 5.5; prefer 5.5.", status="previous", price_in=1, price_out=5),
    # ---------------------------------------------------------------- OpenAI
    _m("gpt-6.2-sol", "GPT-6.2 Sol", "OpenAI", _OPENAI, "excellent",
       "#2 on Roboflow's OCR leaderboard (87.5%, Nov 2026), close behind Gemini 4 Neon. OpenAI's everyday model.",
       evidence="benchmark", price_in=2, price_out=10),
    _m("gpt-6.2-astra", "GPT-6.2 Astra", "OpenAI", _OPENAI, "good",
       "OpenAI's most capable reasoning model. Extra reasoning rarely helps plain transcription "
       "(GPT-6.1 Sol scored no better at high effort), so Sol is usually the better buy.",
       price_in=8, price_out=40),
    _m("gpt-6.1-terra", "GPT-6.1 Terra", "OpenAI", _OPENAI, "good",
       "Described by OpenAI as tuned for visual reasoning; cheap. A good bulk option.", price_in=1, price_out=5),
    _m("gpt-6.1-sol", "GPT-6.1 Sol", "OpenAI", _OPENAI, "excellent",
       "92.0% on Roboflow's OCR benchmark (Oct 2026); higher reasoning effort did not improve it.",
       evidence="benchmark", status="previous", price_in=2, price_out=10),
    _m("gpt-6-luna", "GPT-6 Luna", "OpenAI", _OPENAI, "fair",
       "OpenAI's most efficient model; fine for clean printed text at very low cost.", price_in=0.1, price_out=0.5),
    _m("gpt-6-sol", "GPT-6 Sol", "OpenAI", _OPENAI, "good", "Previous generation.", status="previous",
       price_in=2, price_out=10),
    _m("gpt-6-astra", "GPT-6 Astra", "OpenAI", _OPENAI, "good", "Previous generation; expensive.",
       status="previous", price_in=10, price_out=50),
    _m("gpt-5.6-sol", "GPT-5.6 Sol", "OpenAI", _OPENAI, "good", "Older generation.", status="previous",
       price_in=4, price_out=20),
    _m("gpt-5.6-terra", "GPT-5.6 Terra", "OpenAI", _OPENAI, "good", "Older generation.", status="previous",
       price_in=2, price_out=12),
    _m("gpt-5.6-luna", "GPT-5.6 Luna", "OpenAI", _OPENAI, "fair", "Older budget model.", status="previous",
       price_in=0.2, price_out=1.2),
    _m("gpt-5.5", "GPT-5.5", "OpenAI", _OPENAI, "good", "Older generation; pricey for what it does here.",
       status="previous", price_in=5, price_out=30),
    _m("gpt-5.4", "GPT-5.4", "OpenAI", _OPENAI, "fair", "Older generation.", status="previous",
       price_in=2.5, price_out=15),
    _m("gpt-5.4-mini", "GPT-5.4 mini", "OpenAI", _OPENAI, "fair", "Older budget model.", status="previous",
       price_in=0.75, price_out=4.5),
    _m("gpt-5.4-nano", "GPT-5.4 nano", "OpenAI", _OPENAI, "fair", "Deprecated by OpenAI.", status="deprecated",
       price_in=0.2, price_out=1.25),
    _m("gpt-5", "GPT-5", "OpenAI", _OPENAI, "fair",
       "Deprecated by OpenAI; move to gpt-6.2-sol (similar price, much better at OCR).", status="deprecated",
       price_in=1.25, price_out=10),
    _m("gpt-5-mini", "GPT-5 mini", "OpenAI", _OPENAI, "fair", "Deprecated by OpenAI; try gpt-6.1-terra.",
       status="deprecated", price_in=0.25, price_out=2),
    _m("gpt-5-nano", "GPT-5 nano", "OpenAI", _OPENAI, "fair", "Deprecated by OpenAI; try gpt-6-luna.",
       status="deprecated", price_in=0.05, price_out=0.4),
    _m("gpt-4.1", "GPT-4.1", "OpenAI", _OPENAI, "fair", "Deprecated by OpenAI.", status="deprecated"),
    _m("gpt-4o", "GPT-4o", "OpenAI", _OPENAI, "fair", "Deprecated by OpenAI.", status="deprecated"),
    _m("gpt-4o-mini", "GPT-4o mini", "OpenAI", _OPENAI, "fair", "Deprecated by OpenAI.", status="deprecated"),
    _m("gpt-oss-120b", "gpt-oss-120b", "OpenAI (open weights)", _OPEN, "unsuitable",
       "Text-only open-weight model: it cannot read images."),
    _m("text-embedding-3-large", "text-embedding-3-large", "OpenAI", _OPENAI, "unsuitable",
       "Embedding model: no image input, no text output."),
    _m("whisper-1", "Whisper", "OpenAI", _OPENAI, "unsuitable", "Speech-to-text: audio only."),
    _m("gpt-image-2.6-sunburst", "GPT Image 2.6", "OpenAI", _OPENAI, "unsuitable",
       "Image generator: makes pictures, doesn't transcribe them."),
    # ---------------------------------------------------------------- Google
    _m("gemini-4-neon", "Gemini 4 Neon", "Google", _GEMINI, "excellent",
       "#1 on Roboflow's OCR leaderboard (88.1%, Nov 19 2026, preliminary single run).",
       evidence="benchmark"),
    _m("gemini-4-argon", "Gemini 4 Argon", "Google", _GEMINI, "excellent",
       "Efficient Gemini 4 reasoning model; led an earlier snapshot of the same leaderboard (85.7%).",
       evidence="benchmark", price_in=2, price_out=10),
    _m("gemini-4-carbon", "Gemini 4 Carbon", "Google", _GEMINI, "good",
       "Google's strongest reasoning model. Not separately benchmarked; Neon/Argon are better value for OCR."),
    _m("gemini-4-hydrogen", "Gemini 4 Hydrogen", "Google", _GEMINI, "good",
       "Google's suggested default for new projects; everyday tier."),
    _m("gemini-3.1-pro-preview", "Gemini 3.1 Pro (preview)", "Google", _GEMINI, "excellent",
       "92.6% on Roboflow's OCR benchmark and 95.9% on data extraction (Oct 2026); fast (~6 s/page). "
       "Best closed model on OmniDocBench in Docsumo's roundup.", evidence="benchmark", status="preview",
       price_in=2, price_out=12),
    _m("gemini-3.8-flash", "Gemini 3.8 Flash", "Google", _GEMINI, "good",
       "Cheap, fast and capable; a sensible bulk default.", price_in=0.83, price_out=4.13),
    _m("gemini-3.6-flash", "Gemini 3.6 Flash", "Google", _GEMINI, "good", "Balanced speed and multimodal quality.",
       status="previous", price_in=0.75, price_out=3.75),
    _m("gemini-3-flash-preview", "Gemini 3 Flash (preview)", "Google", _GEMINI, "good", "Older preview.",
       status="preview", price_in=0.5, price_out=3),
    _m("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "Google", _GEMINI, "fair",
       "Lowest-cost current Gemini; clean printed pages only.", price_in=0.3, price_out=2.5),
    _m("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", "Google", _GEMINI, "fair", "Older budget model.",
       status="previous", price_in=0.25, price_out=1.5),
    _m("gemini-2.5-pro", "Gemini 2.5 Pro", "Google", _GEMINI, "fair",
       "Deprecated: from Dec 1, 2026 Google routes it to gemini-3.8-flash.", status="deprecated",
       price_in=1.25, price_out=10),
    _m("gemini-2.5-flash", "Gemini 2.5 Flash", "Google", _GEMINI, "fair",
       "Deprecated: from Dec 1, 2026 Google routes it to gemini-3.5-flash-lite.", status="deprecated",
       price_in=0.3, price_out=2.5),
    _m("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", "Google", _GEMINI, "fair",
       "Old but still served; very cheap.", status="previous", price_in=0.1, price_out=0.4),
    _m("gemini-3-pro-image", "Nano Banana Pro (Gemini 3 Pro Image)", "Google", _GEMINI, "unsuitable",
       "Image generator: outputs pictures, not transcriptions."),
    # ------------------------------------------------- open weights (Ollama, vLLM, OpenRouter, ...)
    _m("kimi-k3", "Kimi K3", "Moonshot AI", _OPEN, "excellent",
       "Best open model in Roboflow's OCR test (93.0%), but 2.8T parameters: realistic only via a hosted API.",
       evidence="benchmark", name_varies=True),
    _m("qwen3.8-max", "Qwen3.8 Max", "Alibaba", _OPEN, "excellent",
       "93.4% on Roboflow's OCR benchmark at a low price; slower than peers (~14 s/page). Hosted only.",
       evidence="benchmark", name_varies=True),
    _m("qwen3.8-27b", "Qwen3.8 27B", "Alibaba", _OPEN, "excellent",
       "92.2% on Roboflow's OCR benchmark; the practical self-hosted choice (fits one large GPU).",
       evidence="benchmark", name_varies=True, aliases=("qwen3.8:27b",)),
    _m("gemma-4-31b", "Gemma 4 31B", "Google (open weights)", _OPEN, "good",
       "90.8% on Roboflow's OCR benchmark; permissive licence, runs locally.", evidence="benchmark",
       name_varies=True, aliases=("gemma4:31b",)),
    _m("glm-ocr", "GLM-OCR (0.9B)", "Zhipu AI", _OPEN, "excellent",
       "Tiny dedicated OCR model (95.2% on OmniDocBench). Runs on a laptop; may ignore custom prompts.",
       evidence="benchmark", name_varies=True),
    _m("paddleocr-vl", "PaddleOCR-VL (0.9B)", "Baidu", _OPEN, "excellent",
       "Tiny dedicated document model that tops OmniDocBench (~96). Needs a server that exposes it via "
       "the OpenAI chat API (e.g. vLLM).", evidence="benchmark", name_varies=True),
    _m("qwen3-vl", "Qwen3-VL", "Alibaba", _OPEN, "good",
       "Widely available on Ollama/vLLM; the 235B version scores ~90 on OmniDocBench, small sizes less.",
       evidence="benchmark", status="previous", name_varies=True),
    _m("qwen2.5vl", "Qwen2.5-VL", "Alibaba", _OPEN, "fair", "Older local vision model (e.g. qwen2.5vl:7b on Ollama).",
       status="previous", name_varies=True),
    _m("llama3.2-vision", "Llama 3.2 Vision", "Meta", _OPEN, "fair",
       "Older local vision model; often paraphrases or skips text.", status="previous", name_varies=True),
    _m("llama-4-maverick", "Llama 4 Maverick", "Meta", _OPEN, "fair", "Older open model.", status="previous",
       name_varies=True),
    _m("mistral-small-3.1", "Mistral Small 3.1 24B", "Mistral", _OPEN, "fair", "Older open vision model.",
       status="previous", name_varies=True),
]

# Name fragments that mark a model which cannot transcribe a page image.
_UNSUITABLE_RE = re.compile(
    r"embed|rerank|tts|whisper|transcribe|moderation|dall-e|gpt-image|imagen|veo|lyria|"
    r"realtime|[-_]audio|audio[-_]|[-_]live|speech|[-_]image(?:$|[-_])|nano-banana|computer-use|codex|search-preview",
)
_VISION_RE = re.compile(r"vl|vision|ocr|pixtral|llava|omni|gemma[-_]?[34]|minicpm-v|molmo")
# What may follow a catalog id inside a longer name: a tag, a date, or a quantisation / variant
# suffix (Ollama "qwen3.8:27b-q4_K_M", HF "...-Instruct-AWQ"); not another version digit.
_TRAILER_RE = r"(?=$|[:@/]|-\d{4,}|-(?:latest|q\d|fp\d|bf16|int\d|instruct|it|awq|gptq|gguf|mlx)(?:$|[-.]))"


def _norm(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _patterns(info: ModelInfo) -> list[str]:
    return [_norm(n) for n in (info.id, *info.aliases)]


def lookup(name: str, provider_id: str | None = None) -> dict:
    """Describe how well ``name`` suits page transcription.

    Matches the catalog exactly first, then loosely, so ``openai/gpt-6.2-sol`` (OpenRouter),
    ``prod-gpt-6.2-sol`` (an Azure deployment) or ``qwen3.8:27b-q4`` (an Ollama tag) are
    recognised. Unknown names get a heuristic verdict and ``match: None``.
    """
    n = _norm(name)
    if not n:
        return {"name": name, "match": None, "rating": "unrated", "rating_label": RATINGS["unrated"][0],
                "note": ""}
    best: tuple[int, ModelInfo] | None = None
    for info in MODELS:
        for pat in _patterns(info):
            if n == pat:
                best = (10_000, info)
                break
            if re.search(r"(?:^|[/:._-])" + re.escape(pat) + _TRAILER_RE, n):
                if best is None or len(pat) > best[0]:
                    best = (len(pat), info)
        if best and best[0] == 10_000:
            break
    if best:
        info = best[1]
        out = {"name": name, "match": info.id, "exact": best[0] == 10_000, **info.to_json()}
        out["id"] = info.id
        if provider_id and provider_id not in info.providers:
            out["note"] = f"{info.note} (Usually not offered by this provider.)"
        return out
    if _UNSUITABLE_RE.search(n):
        rating, note = "unsuitable", "Looks like a non-vision model (embeddings, audio, image generation, code...)."
    elif _VISION_RE.search(n):
        rating, note = "unrated", "Not in the catalog, but the name suggests a vision model. Try Test connection."
    else:
        rating, note = "unrated", "Not in the catalog. Make sure it accepts images, then try Test connection."
    return {"name": name, "match": None, "rating": rating, "rating_label": RATINGS[rating][0], "note": note}


def price(name: str, provider_id: str | None = None) -> tuple[float, float, str] | None:
    """``(usd_per_1m_in, usd_per_1m_out, catalog_id)`` for a model name, or None if unknown."""
    info = lookup(name, provider_id)
    if info.get("match") and info.get("price_in") is not None and info.get("price_out") is not None:
        return info["price_in"], info["price_out"], info["match"]
    return None


def _sort_key(info: ModelInfo) -> tuple:
    status_order = {"current": 0, "preview": 1, "previous": 2, "deprecated": 3}
    return (RATINGS[info.rating][1], status_order.get(info.status, 9), info.price_in or 0)


def search(q: str = "", provider_id: str | None = None, rating: str | None = None,
           include_deprecated: bool = True) -> list[ModelInfo]:
    """Filter the catalog by free text (id, label, vendor, note), provider and rating."""
    words = [w for w in _norm(q).split("-") if w] if q else []
    out = []
    for info in MODELS:
        if provider_id and provider_id not in info.providers:
            continue
        if rating and info.rating != rating:
            continue
        if not include_deprecated and info.status == "deprecated":
            continue
        hay = _norm(" ".join([info.id, info.label, info.vendor, info.note, info.status, *info.aliases]))
        if all(w in hay for w in words):
            out.append(info)
    return sorted(out, key=_sort_key)


def recommended(provider_id: str, limit: int = 6) -> list[str]:
    """Model ids worth suggesting first for a provider (current, rated good or better)."""
    picks = [m for m in search(provider_id=provider_id)
             if m.rating in ("excellent", "good") and m.status in ("current", "preview") and not m.name_varies]
    return [m.id for m in picks[:limit]]


def model_options(provider_id: str, suggested: list[str]) -> list[dict]:
    """What a model input should offer: the provider's suggestions, then the rest of the catalog.

    Unsuitable and deprecated models are left out of the dropdown (they're still searchable on the
    Models page and still accepted if typed). Families whose name varies per server are left out
    too, since their catalog id is not something a server will recognise.
    """
    out, seen = [], set()
    for name in suggested:
        if name and name not in seen:
            seen.add(name)
            out.append(lookup(name, provider_id))
    for m in search(provider_id=provider_id, include_deprecated=False):
        if m.id in seen or m.rating == "unsuitable" or m.name_varies:
            continue
        seen.add(m.id)
        out.append(lookup(m.id, provider_id))
    return out
