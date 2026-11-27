"""Cloud OCR services: Google Cloud Vision, Azure AI Document Intelligence,
AWS Textract and Mistral OCR.

These send the rendered page image to the service the user configured, and
nowhere else. Google, Azure and Mistral are plain REST calls through
``httpx`` (tests swap in a mock transport). Textract uses boto3, because
AWS request signing is not worth re-implementing.

Errors become :class:`~pyocr_toolkit.llm.ProviderError` (HTTP 502) with a
message saying what to fix: a bad key, an unreachable endpoint, a size limit.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from ..documents import RenderedImage, Word
from ..llm.base import ProviderError
from .base import OCREngine, OCRResult, pairs, rect_of, split_line


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    err = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or err)[:300]
    return str(err)[:300]


class _HTTPEngine(OCREngine):
    kind = "cloud"
    concurrency = 4

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._http: httpx.Client | None = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.settings.ocr_timeout_s)
        return self._http

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code in (401, 403):
            raise ProviderError(f"{self.label} rejected the credentials ({resp.status_code}): {_error_text(resp)}. "
                                f"Check {', '.join(n for n, req, _ in self.env_vars if req)}.")
        if resp.status_code == 413:
            raise ProviderError(f"{self.label}: the page image is too large. Lower OCR_DPI or OCR_MAX_EDGE_PX.")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.label} {what} failed ({resp.status_code}): {_error_text(resp)}")
        return resp

    def _send(self, method: str, url: str, what: str, **kw) -> httpx.Response:
        try:
            resp = self.http.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not reach {self.label} ({exc.__class__.__name__}: {exc})") from exc
        return self._check(resp, what)


# ------------------------------------------------------------------ Google
class GoogleVisionEngine(_HTTPEngine):
    id = "google_vision"
    label = "Google Cloud Vision"
    brand = "google_cloud"
    description = "Google's DOCUMENT_TEXT_DETECTION. Excellent on dense print and handwriting; auto-detects language."
    docs_url = "https://cloud.google.com/vision/docs/fulltext-annotations"
    env_vars = [("GOOGLE_VISION_API_KEY", True, "API key with the Cloud Vision API enabled"),
                ("GOOGLE_VISION_LANGUAGE_HINTS", False, "Optional, e.g. en,de (empty = auto-detect)")]
    # 10 MB JSON request limit, and base64 adds a third
    max_image_bytes = 7_000_000
    url = "https://vision.googleapis.com/v1/images:annotate"

    def missing_settings(self) -> list[str]:
        return [] if self.settings.google_vision_api_key else ["GOOGLE_VISION_API_KEY"]

    def hints(self) -> list[str]:
        return [h.strip() for h in self.settings.google_vision_language_hints.split(",") if h.strip()]

    def settings_summary(self) -> str:
        return f"hints {','.join(self.hints())}" if self.hints() else "language auto-detect"

    def recognize(self, image: RenderedImage) -> OCRResult:
        req: dict[str, Any] = {"image": {"content": _b64(image.data)},
                               "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]}
        if self.hints():
            req["imageContext"] = {"languageHints": self.hints()}
        resp = self._send("POST", self.url, "request", json={"requests": [req]},
                          headers={"x-goog-api-key": self.settings.google_vision_api_key or ""})
        out = (resp.json().get("responses") or [{}])[0]
        if "error" in out:
            raise ProviderError(f"Google Cloud Vision: {out['error'].get('message', out['error'])}")
        return parse_google_vision(out)


def _vertices(poly: dict) -> list[tuple[float, float]]:
    # zero coordinates are omitted from the JSON
    return [(v.get("x", 0), v.get("y", 0)) for v in (poly or {}).get("vertices", [])] or [(0, 0)]


def parse_google_vision(out: dict) -> OCRResult:
    fta = out.get("fullTextAnnotation") or {}
    words: list[Word] = []
    line = 0
    for page in fta.get("pages", [])[:1]:
        for b, block in enumerate(page.get("blocks", [])):
            for para in block.get("paragraphs", []):
                for w in para.get("words", []):
                    symbols = w.get("symbols", [])
                    text = "".join(s.get("text", "") for s in symbols)
                    if text:
                        words.append(Word(*rect_of(_vertices(w.get("boundingBox"))), text, b, line))
                    brk = ((symbols[-1].get("property") or {}).get("detectedBreak") or {}).get("type") if symbols else None
                    if brk in ("EOL_SURE_SPACE", "LINE_BREAK", "HYPHEN"):
                        line += 1
                line += 1
    return OCRResult(words=words, text=fta.get("text", ""))


# ------------------------------------------------------------------ Azure
class AzureDocumentIntelligenceEngine(_HTTPEngine):
    id = "azure_di"
    label = "Azure AI Document Intelligence"
    brand = "azure"
    description = "Microsoft's prebuilt-read model. Strong on print and handwriting, returns words and lines."
    docs_url = "https://learn.microsoft.com/azure/ai-services/document-intelligence/prebuilt/read"
    env_vars = [("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", True, "https://<resource>.cognitiveservices.azure.com"),
                ("AZURE_DOCUMENT_INTELLIGENCE_KEY", True, "Key 1 or 2 of the resource")]
    # the free (F0) tier accepts files up to 4 MB
    max_image_bytes = 3_900_000
    api_version = "2024-11-30"
    poll_interval_s = 1.0

    def missing_settings(self) -> list[str]:
        s = self.settings
        return [n for n, v in (("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", s.azure_document_intelligence_endpoint),
                               ("AZURE_DOCUMENT_INTELLIGENCE_KEY", s.azure_document_intelligence_key)) if not v]

    def settings_summary(self) -> str:
        return f"prebuilt-read · api {self.api_version}"

    def recognize(self, image: RenderedImage) -> OCRResult:
        s = self.settings
        endpoint = (s.azure_document_intelligence_endpoint or "").rstrip("/")
        headers = {"Ocp-Apim-Subscription-Key": s.azure_document_intelligence_key or ""}
        url = f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze?api-version={self.api_version}"
        resp = self._send("POST", url, "analyze request", json={"base64Source": _b64(image.data)}, headers=headers)
        op_url = resp.headers.get("Operation-Location")
        if not op_url:
            raise ProviderError("Azure Document Intelligence did not return an Operation-Location header")
        deadline = time.monotonic() + s.ocr_timeout_s
        while True:
            poll = self._send("GET", op_url, "result poll", headers=headers)
            op = poll.json()
            status = op.get("status")
            if status == "succeeded":
                return parse_azure_read(op.get("analyzeResult") or {}, image.width_px)
            if status in ("failed", "canceled", "skipped"):
                err = (op.get("error") or {}).get("message", status)
                raise ProviderError(f"Azure Document Intelligence analysis {status}: {err}")
            if time.monotonic() > deadline:
                raise ProviderError(f"Azure Document Intelligence did not finish within {s.ocr_timeout_s:.0f}s")
            try:
                wait = float(poll.headers.get("Retry-After") or resp.headers.get("Retry-After") or self.poll_interval_s)
            except ValueError:
                wait = self.poll_interval_s
            time.sleep(min(max(wait, 0.0), 5.0))


def parse_azure_read(result: dict, width_px: int) -> OCRResult:
    pages = result.get("pages") or []
    if not pages:
        return OCRResult(words=[], text=result.get("content", ""))
    page = pages[0]
    # Units are pixels for images (inches for PDFs); scaling by the page width
    # works for either.
    scale = width_px / page["width"] if page.get("width") else 1.0
    # A word belongs to the line whose span covers the word's offset.
    line_ranges = []
    for i, ln in enumerate(page.get("lines") or []):
        for sp in ln.get("spans") or []:
            line_ranges.append((sp["offset"], sp["offset"] + sp["length"], i))
    words: list[Word] = []
    for w in page.get("words") or []:
        off = (w.get("span") or {}).get("offset", -1)
        line = next((i for a, b, i in line_ranges if a <= off < b), len(line_ranges) + len(words))
        x0, y0, x1, y1 = rect_of(pairs(w.get("polygon") or [0, 0]))
        words.append(Word(x0 * scale, y0 * scale, x1 * scale, y1 * scale, w.get("content", ""), 0, line))
    return OCRResult(words=words, text=result.get("content"), model=result.get("modelId"))


# ------------------------------------------------------------------ AWS
class TextractEngine(OCREngine):
    id = "textract"
    label = "AWS Textract"
    kind = "cloud"
    brand = "aws"
    description = "Amazon's DetectDocumentText. Reliable on forms, receipts and scans; returns lines and words."
    docs_url = "https://docs.aws.amazon.com/textract/latest/dg/detecting-document-text.html"
    env_vars = [("AWS_REGION", True, "e.g. us-east-1"),
                ("AWS_ACCESS_KEY_ID", False, "Optional: otherwise ~/.aws/credentials, AWS_PROFILE or a role"),
                ("AWS_SECRET_ACCESS_KEY", False, "Optional, with AWS_ACCESS_KEY_ID"),
                ("AWS_SESSION_TOKEN", False, "Optional, for temporary credentials")]
    requires = [("boto3",)]
    pip_extra = "textract"
    concurrency = 4
    # synchronous calls accept documents up to 10 MB
    max_image_bytes = 9_500_000

    def missing_settings(self) -> list[str]:
        return [] if self.settings.aws_region else ["AWS_REGION"]

    def settings_summary(self) -> str:
        return f"region {self.settings.aws_region}" if self.settings.aws_region else ""

    def _client(self):
        import boto3

        s = self.settings
        return boto3.client("textract", region_name=s.aws_region, aws_access_key_id=s.aws_access_key_id or None,
                            aws_secret_access_key=s.aws_secret_access_key or None,
                            aws_session_token=s.aws_session_token or None)

    def recognize(self, image: RenderedImage) -> OCRResult:
        client = self._with_model(self._client)  # boto3 clients are thread-safe
        try:
            resp = client.detect_document_text(Document={"Bytes": image.data})
        except Exception as exc:  # botocore raises many types (ClientError, NoCredentialsError, ...)
            hint = " Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or configure ~/.aws." \
                if "credential" in str(exc).lower() else ""
            raise ProviderError(f"AWS Textract failed: {exc}.{hint}") from exc
        return parse_textract(resp, image.width_px, image.height_px)


def parse_textract(resp: dict, width: int, height: int) -> OCRResult:
    """LINE blocks come in reading order; their CHILD relationships list the WORD blocks.

    Bounding boxes are ratios of the page size.
    """
    blocks = resp.get("Blocks") or []
    by_id = {b["Id"]: b for b in blocks if "Id" in b}
    words: list[Word] = []
    lines: list[str] = []

    def box(b: dict) -> tuple[float, float, float, float]:
        bb = (b.get("Geometry") or {}).get("BoundingBox") or {}
        x0, y0 = bb.get("Left", 0) * width, bb.get("Top", 0) * height
        return x0, y0, x0 + bb.get("Width", 0) * width, y0 + bb.get("Height", 0) * height

    for i, ln in enumerate(b for b in blocks if b.get("BlockType") == "LINE"):
        lines.append(ln.get("Text", ""))
        child_ids = [cid for r in ln.get("Relationships") or [] if r.get("Type") == "CHILD" for cid in r.get("Ids", [])]
        children = [by_id[c] for c in child_ids if c in by_id and by_id[c].get("BlockType") == "WORD"]
        if children:
            words += [Word(*box(w), w.get("Text", ""), 0, i) for w in children]
        else:
            words += split_line(*box(ln), ln.get("Text", ""), 0, i)
    return OCRResult(words=words, text="\n".join(lines) + ("\n" if lines else ""),
                     model=resp.get("DetectDocumentTextModelVersion"))


# ------------------------------------------------------------------ Mistral
class MistralOCREngine(_HTTPEngine):
    id = "mistral"
    label = "Mistral OCR"
    brand = "mistral"
    description = ("Mistral's document OCR model. Returns Markdown (headings, tables) but no word boxes, "
                   "so search can't highlight its hits on the page image.")
    docs_url = "https://docs.mistral.ai/capabilities/document_ai/basic_ocr/"
    env_vars = [("MISTRAL_API_KEY", True, "API key from console.mistral.ai"),
                ("MISTRAL_OCR_MODEL", False, "OCR model id")]
    has_boxes = False
    max_image_bytes = 7_000_000
    url = "https://api.mistral.ai/v1/ocr"

    def missing_settings(self) -> list[str]:
        return [] if self.settings.mistral_api_key else ["MISTRAL_API_KEY"]

    def settings_summary(self) -> str:
        return f"model {self.settings.mistral_ocr_model}"

    def recognize(self, image: RenderedImage) -> OCRResult:
        body = {"model": self.settings.mistral_ocr_model,
                "document": {"type": "image_url", "image_url": f"data:{image.mime};base64,{_b64(image.data)}"}}
        resp = self._send("POST", self.url, "request", json=body,
                          headers={"Authorization": f"Bearer {self.settings.mistral_api_key}"})
        data = resp.json()
        text = "\n\n".join((p.get("markdown") or "").strip() for p in data.get("pages") or [])
        return OCRResult(words=[], text=text + ("\n" if text else ""), model=data.get("model"))
