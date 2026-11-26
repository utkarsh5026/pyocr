"""
Demo data for the docs' tours and hero video: sample documents, a stand-in vision model, and a seeded app.

    python .claude/skills/demo/demo_data.py --serve           # seeded app on http://127.0.0.1:8765, Ctrl-C to stop
    python .claude/skills/demo/demo_data.py --docs <folder>   # only write the sample documents

Three documents, all made here so the docs never depend on a real customer file:

- ``harbor-q3-report.pdf``: born-digital, 4 pages. Its embedded text is ground truth. It contains the misspelling
  "recieved", which the stand-in model quietly "fixes", as real vision models often do, so the Compare view has
  something true to show.
- ``field-notes-scan.pdf``: 3 pages of typed notes printed, scanned (slightly rotated, blurred and noisy) and saved
  as images only. It has no embedded text, so the app flags it ``scan``; Tesseract makes real mistakes on it.
- ``supplier-invoice-scan.pdf``: a 2-page scan the upload tour drops in and runs OCR on.

The stand-in model is a small OpenAI-compatible server on localhost (``/v1/chat/completions``), registered as the
app's "OpenAI-compatible" provider with the model name ``demo-vision``. It recognises which page it was sent by
comparing a thumbnail with every sample page, and answers with that page's true text (with the "fix" above), after a
short pause so jobs show progress. No API key and no network is needed. It is a stand-in: the docs say so wherever
an LLM result shows.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

MODEL = "demo-vision"
PROVIDER = "openai_compatible"
LLM_PAUSE_S = (0.7, 1.3)
THUMB = (24, 32)

# ---------------------------------------------------------------------------------------------------- documents

REPORT_PAGES = [
    """Harbor Instruments - Quarterly Report, Q3 2025

Summary
Revenue for the quarter was $4.82M, up 11% on Q2. Orders recieved in
September alone reached 1,204, the highest month on record. Gross margin
held at 46.3% despite higher freight costs on the NB-0417/ii sensor line.

Highlights
- Shipped firmware 3.9 to all 2,310 field units without a recall.
- Opened the Rotterdam service desk on 2 September.
- Cut average repair turnaround from 9.4 to 6.1 days.""",
    """Revenue by product line (USD thousands)

Line            Q1        Q2        Q3      Change
NB-0417/ii    1,210     1,342     1,498     +11.6%
TX-22 probes    880       905     1,011     +11.7%
Field kits      640       702       744      +6.0%
Service         960     1,391     1,567     +12.7%
Total         3,690     4,340     4,820     +11.1%

Note: Service includes the Rotterdam desk from 2 September. Kits sold
with a 3-year plan are booked under Field kits, not Service.""",
    """Operations

Units built: 2,940 (target 2,800). Line 2 ran a second shift from week 31.
Scrap rate fell to 1.8% after the new torque fixture on station 4.
Supplier lead time for the TX-22 housing is still 14 weeks; we have
recieved a quote from a second supplier at 9 weeks and +4% unit cost.

Risks
1. One supplier for the NB-0417/ii optical window.
2. Freight rates on the Asia-Europe lane up 18% since June.
3. Two senior field engineers leave in Q4; hiring is under way.""",
    """Outlook for Q4

We expect revenue of $5.1M to $5.4M. The TX-22 refresh launches on
14 November, and pre-orders already cover 38% of the first batch.

Contacts
Finance: finance@harbor-instruments.example
Investor relations: ir@harbor-instruments.example

Harbor Instruments B.V. - Kade 12, 3011 AB Rotterdam""",
]

NOTES_PAGES = [
    """FIELD NOTES - SITE 7, NORTH PIER
Date: 14 Aug 2025   Recorded by: M. Okafor

09:10  Arrived on site. Tide falling, wind SW 12 kn.
09:25  Sensor NB-0417/ii found tilted ~4 deg. Re-seated
       and torqued to 6 Nm. Reading before: 0.731 V,
       after: 0.702 V.
10:05  Sampled water at buoys B1, B2, B4 (B3 missing).
10:40  Photo set 0193-0221 taken of the pier footing.""",
    """11:15  Logger battery at 61%. Swapped for spare #S-08.
11:30  Firmware on logger reads 3.8.2, not 3.9 as listed
       in the asset register. Flag for office.
12:00  Lunch.
13:20  Found 3 cm crack in concrete at pile 12, east face.
       Marked with yellow paint, photos 0222-0230.

Readings (mg/L):   B1 7.9   B2 8.1   B4 6.4""",
    """ACTIONS
[ ] Order replacement buoy B3 (part TX-22-B, qty 1)
[ ] Update asset register: logger firmware 3.8.2
[x] Report crack at pile 12 to structures team
[ ] Re-check NB-0417/ii tilt in 2 weeks (28 Aug)

Signed: M. Okafor         Checked: J. Lindqvist""",
]

INVOICE_PAGES = [
    """INVOICE  No. 2025-0917
Northgate Optics Ltd                    Date: 17 Sep 2025
Unit 4, Mill Lane, Leeds LS2 7AB        Due:  17 Oct 2025

Bill to: Harbor Instruments B.V., Kade 12, Rotterdam

Qty  Item                               Unit      Total
 40  Optical window OW-17 (NB-0417/ii)  38.50   1,540.00
 12  Window gasket G-4                   2.10      25.20
  1  Freight, Leeds - Rotterdam         86.00      86.00""",
    """                                   Subtotal  1,651.20
                                   VAT 0%        0.00
                                   TOTAL EUR 1,651.20

Payment: IBAN GB00 NWBK 0000 0000 0000 00 (example)
Reference: 2025-0917

Thank you for your order. Goods remain our property
until paid in full.""",
]


@dataclass
class SampleDoc:
    filename: str
    pages: list[str]
    scanned: bool

    def transcript(self, page: int) -> str:
        """What the stand-in model returns for a page: the true text, with misspellings quietly "fixed"."""
        return self.pages[page].replace("recieved", "received")


DOCS = [
    SampleDoc("harbor-q3-report.pdf", REPORT_PAGES, scanned=False),
    SampleDoc("field-notes-scan.pdf", NOTES_PAGES, scanned=True),
    SampleDoc("supplier-invoice-scan.pdf", INVOICE_PAGES, scanned=True),
]


def _typeset(text: str, *, fontname: str, fontsize: float, title_size: float | None = None) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4
    y = 72.0
    for i, line in enumerate(text.split("\n")):
        size = title_size if (i == 0 and title_size) else fontsize
        page.insert_text((64, y), line, fontname=fontname, fontsize=size)
        y += size * 1.55 + (8 if i == 0 and title_size else 0)
    return doc


def write_born_digital(sample: SampleDoc, path: Path) -> None:
    out = pymupdf.open()
    for text in sample.pages:
        out.insert_pdf(_typeset(text, fontname="helv", fontsize=11.5, title_size=17))
    out.set_metadata({"title": sample.filename, "author": "pyocr-toolkit demo", "producer": "pyocr-toolkit demo"})
    out.save(path)


def write_scan(sample: SampleDoc, path: Path, *, seed: int) -> None:
    """Typeset each page in a typewriter face, then make it look photocopied: rotate, blur, add noise and grain."""
    rng = random.Random(seed)
    out = pymupdf.open()
    for n, text in enumerate(sample.pages):
        pix = _typeset(text, fontname="cour", fontsize=12)[0].get_pixmap(dpi=200, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        img = img.rotate(rng.uniform(-0.9, 0.9) + (0.4 if n % 2 else -0.3), resample=Image.BICUBIC, fillcolor=255,
                         expand=False)
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        px = img.load()
        w, h = img.size
        for _ in range(w * h // 5000):  # speckle
            x, y = rng.randrange(w), rng.randrange(h)
            px[x, y] = rng.choice((0, 60, 120, 200))
        paper = Image.new("L", img.size, 0)
        img = Image.blend(img, paper, 0.04)
        rgb = Image.merge("RGB", (img, img, img.point(lambda v: max(0, v - 10))))
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=72)
        page = out.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.getvalue())
    out.set_metadata({"title": sample.filename, "producer": "scanner (pyocr-toolkit demo)"})
    out.save(path)


def write_docs(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for i, sample in enumerate(DOCS):
        path = folder / sample.filename
        (write_scan(sample, path, seed=i) if sample.scanned else write_born_digital(sample, path))
        paths[sample.filename] = path
    return paths


# ---------------------------------------------------------------------------------------------------- stand-in model


def _thumb(image: Image.Image) -> list[int]:
    return list(image.convert("L").resize(THUMB, Image.BILINEAR).tobytes())


class StandInModel:
    """An OpenAI-compatible chat endpoint that 'transcribes' a sample page by recognising it from a thumbnail."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self.index: list[tuple[list[int], str]] = []
        for sample in DOCS:
            with pymupdf.open(paths[sample.filename]) as doc:
                for n, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=40, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    self.index.append((_thumb(img), sample.transcript(n)))
        self.server = ThreadingHTTPServer(("127.0.0.1", free_port()), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def answer(self, image_bytes: bytes) -> str:
        probe = _thumb(Image.open(io.BytesIO(image_bytes)))
        return min(self.index, key=lambda item: sum((a - b) ** 2 for a, b in zip(item[0], probe)))[1]

    def _handler(self):
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):  # /v1/models, for "Test connection"
                self._send({"object": "list", "data": [{"id": MODEL, "object": "model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                url = next(part["image_url"]["url"] for msg in body["messages"] if isinstance(msg["content"], list)
                           for part in msg["content"] if part.get("type") == "image_url")
                text = model.answer(base64.b64decode(url.split(",", 1)[1]))
                time.sleep(random.uniform(*LLM_PAUSE_S))
                self._send({
                    "id": "chatcmpl-demo", "object": "chat.completion", "created": int(time.time()), "model": MODEL,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 1450 + len(url) % 300, "completion_tokens": len(text) // 4,
                              "total_tokens": 1450 + len(text) // 4},
                })

            def _send(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def start(self) -> StandInModel:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()


# ---------------------------------------------------------------------------------------------------- the app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(base: str, method: str, path: str, body: dict | None = None, *, upload: Path | None = None):
    headers, data = {}, None
    if upload is not None:
        boundary = "----pyocrdemo"
        data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{upload.name}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n").encode() + upload.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        headers["content-type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


class DemoApp:
    """The real app, run in a subprocess on an empty data folder, with only the stand-in model configured."""

    def __init__(self, workdir: Path, port: int | None = None) -> None:
        self.workdir = workdir
        self.docs = write_docs(workdir / "samples")
        self.model = StandInModel(self.docs).start()
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        keep = {k: v for k, v in os.environ.items()
                if not re.match(r"(ANTHROPIC|OPENAI|AZURE|GEMINI|GOOGLE)_", k)}
        env = {**keep, "DATA_DIR": str(workdir / "data"), "OPENAI_COMPATIBLE_BASE_URL": self.model.url,
               "OPENAI_COMPATIBLE_MODEL": MODEL, "LLM_CONCURRENCY": "1"}
        # cwd = workdir, so a developer's own .env is not read
        self.proc = subprocess.Popen([sys.executable, "-m", "pyocr_toolkit", "serve", "--port", str(self.port),
                                      "--log-level", "warning"], cwd=workdir, env=env)
        for _ in range(100):
            try:
                _call(self.url, "GET", "/api/config")
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("the app did not start")

    def upload(self, filename: str) -> str:
        return _call(self.url, "POST", "/api/documents", upload=self.docs[filename])["id"]

    def run_job(self, doc_id: str, kind: str, pages: str | None = None) -> None:
        body = {"kind": kind, "pages": pages}
        if kind == "llm":
            body["llm"] = {"provider": PROVIDER, "model": MODEL}
        job = _call(self.url, "POST", f"/api/documents/{doc_id}/jobs", body)
        while job["status"] in ("queued", "running"):
            time.sleep(0.3)
            job = _call(self.url, "GET", f"/api/jobs/{job['id']}")
        if job["status"] != "done":
            raise RuntimeError(f"seed job failed: {job}")

    def seed(self) -> dict[str, str]:
        """Two documents with text from every source, some pages deliberately left without OCR or LLM text."""
        ids = {}
        ids["notes"] = self.upload("field-notes-scan.pdf")
        ids["report"] = self.upload("harbor-q3-report.pdf")
        self.run_job(ids["notes"], "ocr")
        self.run_job(ids["notes"], "llm", "1-2")
        self.run_job(ids["report"], "ocr", "1-3")
        self.run_job(ids["report"], "llm")
        return ids

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)
        self.model.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", help="run the seeded app until Ctrl-C")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--docs", type=Path, help="only write the sample documents to this folder")
    args = parser.parse_args()
    if args.docs:
        for path in write_docs(args.docs).values():
            print(path)
        return 0
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    with tempfile.TemporaryDirectory(prefix="pyocr-demo-") as tmp:
        app = DemoApp(Path(tmp), args.port)
        try:
            print(f"seeding {app.url} ...", file=sys.stderr)
            app.seed()
            print(f"ready: {app.url}  (stand-in model at {app.model.url})", file=sys.stderr)
            if args.serve:
                while True:
                    time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
