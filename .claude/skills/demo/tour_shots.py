"""
Make the docs' animated tours: short videos of the web app being used, as looping animated WebPs.

    python .claude/skills/demo/tour_shots.py                    # every tour, light and dark, into docs/tours/
    python .claude/skills/demo/tour_shots.py compare search     # just these
    python .claude/skills/demo/tour_shots.py --theme dark       # one theme
    python .claude/skills/demo/tour_shots.py --out /tmp/tours   # somewhere else, docs left alone
    python .claude/skills/demo/tour_shots.py --list

Each tour starts the real app on demo data (demo_data.py: sample documents and a stand-in vision model, so no API key
or network is needed), opens it in headless Chromium and plays a scripted visit: a drawn pointer glides to each
button and clicks it with a ring, text is typed a letter at a time, and each result stays on screen long enough to
read. A frame is taken at every step, and every so often while the app is busy (a job running, a page loading), so
the tour shows what a user waits for, sped up. The frames become docs/tours/<name>-{light,dark}.webp: lossless,
looping, used by README.md (light) and the website (light or dark, as the page is). The last frame is also saved as
<name>-{light,dark}.png, for readers who prefer reduced motion and for the hero video (hero-video/).

Needs playwright (the cloud sessions have it; else pip install playwright && playwright install chromium) and
Pillow, plus Tesseract for the OCR in the demo data.
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]

from demo_data import DemoApp  # noqa: E402

ROOT = HERE.parents[2]
OUT = ROOT / "docs" / "tours"
THEMES = ("light", "dark")
VIEWPORT = {"width": 1280, "height": 800}

GLIDE_FRAME_MS = 33
RING_FRAME_MS = 90
KEY_FRAME_MS = 70
BUSY_FRAME_MS = 180
BUSY_POLL_S = 0.45
MAX_BUSY_FRAMES = 60
SCROLL_FRAME_MS = 40

POINTER_HTML = """() => {
  if (document.getElementById('tour-pointer')) return;
  const pointer = document.createElement('div');
  pointer.id = 'tour-pointer';
  pointer.style.cssText = 'position:fixed;left:-2px;top:-2px;z-index:100001;pointer-events:none;'
    + 'filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.35))';
  pointer.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="26" viewBox="0 0 20 26">'
    + '<path d="M2 2v18.6l4.7-4.3 3.3 7.4 3.3-1.5-3.2-7.2h6.6z" fill="#111" stroke="#fff" stroke-width="1.6"'
    + ' stroke-linejoin="round"/></svg>';
  const ring = document.createElement('div');
  ring.id = 'tour-ring';
  ring.style.cssText = 'position:fixed;left:-18px;top:-18px;width:36px;height:36px;border-radius:50%;'
    + 'z-index:100000;pointer-events:none;opacity:0;background:rgba(79,70,229,.25);'
    + 'box-shadow:0 0 0 2px rgba(79,70,229,.65)';
  document.body.append(ring, pointer);
}"""
MOVE_POINTER = """([x, y, ring]) => {
  document.getElementById('tour-pointer').style.transform = `translate(${x}px, ${y}px)`;
  const r = document.getElementById('tour-ring');
  r.style.transform = `translate(${x}px, ${y}px) scale(${ring ? ring : 0.4})`;
  r.style.opacity = ring ? String(1.6 - ring) : '0';
}"""
# Scrollbars flicker between frames, so the recordings hide them.
QUIET_CSS = "::-webkit-scrollbar{display:none} *{scrollbar-width:none !important}"
PAGE_BUSY = "[...document.images].some(i => !i.complete) || document.readyState !== 'complete'"


class Tour:
    """
    Records a visit to the app: each method moves the drawn pointer, acts, and adds frames of the page.
    """

    def __init__(self, page: Any, base: str) -> None:
        self.page = page
        self.base = base
        self.x, self.y = VIEWPORT["width"] * 0.62, VIEWPORT["height"] * 0.55
        self.frames: list[tuple[Any, int]] = []

    # -- frames ---------------------------------------------------------------------------------------------------
    def frame(self, ms: float, ring: float = 0) -> None:
        """Adds the page as it is now, shown for `ms` milliseconds."""
        from PIL import Image

        for attempt in range(3):  # a click that navigates destroys the page under us: wait for the next one
            try:
                self.page.evaluate(POINTER_HTML)
                self.page.evaluate(MOVE_POINTER, [self.x, self.y, ring])
                break
            except Exception:
                if attempt == 2:
                    raise
                self.page.wait_for_load_state("load")
                self.page.add_style_tag(content=QUIET_CSS)
        try:
            shot = self.page.screenshot(timeout=10_000)
        except Exception:  # a screenshot taken mid-navigation (upload redirect, reload after a job) can hang
            self.page.wait_for_load_state("load")
            self.page.add_style_tag(content=QUIET_CSS)
            self.page.evaluate(POINTER_HTML)
            self.page.evaluate(MOVE_POINTER, [self.x, self.y, ring])
            shot = self.page.screenshot()
        picture = Image.open(io.BytesIO(shot)).convert("RGB")
        self.frames.append((picture, round(ms)))

    def wait(self, busy_js: str = PAGE_BUSY, *, settle: float = 0.4) -> None:
        """Adds a frame now and then while `busy_js` is true, and returns once it isn't."""
        time.sleep(0.15)
        for _ in range(MAX_BUSY_FRAMES):
            if not self.page.evaluate(f"() => {busy_js}"):
                break
            self.frame(BUSY_FRAME_MS)
            time.sleep(BUSY_POLL_S)
        self.page.wait_for_function(f"() => !({busy_js})", timeout=120_000)
        time.sleep(settle)

    def hold(self, seconds: float, busy_js: str = PAGE_BUSY) -> None:
        """Stays on what the page shows, once it has finished loading."""
        self.wait(busy_js)
        self.frame(seconds * 1000)

    # -- actions --------------------------------------------------------------------------------------------------
    def goto(self, path: str, *, hold: float = 1.0) -> None:
        self.page.goto(self.base + path)
        self.page.add_style_tag(content=QUIET_CSS)
        self.hold(hold)

    def glide(self, target: Any, *, at: float = 0.5) -> tuple[float, float]:
        """Moves the pointer to the target, `at` of the way across it, and returns where it stopped in the target."""
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        inside = (min(box["width"] * at, 220), box["height"] * 0.5)
        to_x, to_y = box["x"] + inside[0], box["y"] + inside[1]
        distance = ((to_x - self.x) ** 2 + (to_y - self.y) ** 2) ** 0.5
        moves = max(8, min(18, round(distance / 30)))
        start_x, start_y = self.x, self.y
        for i in range(1, moves + 1):
            ease = (i / moves) ** 2 * (3 - 2 * i / moves)
            self.x = start_x + (to_x - start_x) * ease
            self.y = start_y + (to_y - start_y) * ease
            self.page.mouse.move(self.x, self.y)
            self.frame(GLIDE_FRAME_MS)
        return inside

    def click(self, target: Any, *, hold: float = 1.2, at: float = 0.5, busy_js: str = PAGE_BUSY) -> None:
        """Glides to the target and clicks it, then stays on the result."""
        inside = self.glide(target, at=at)
        self.frame(RING_FRAME_MS, 0.8)
        target.click(position={"x": inside[0], "y": inside[1]})
        self.frame(RING_FRAME_MS, 1.2)
        self.hold(hold, busy_js)

    def type(self, box: Any, text: str, *, enter: bool = True, hold: float = 1.5, busy_js: str = PAGE_BUSY) -> None:
        """Clicks into a text box and types `text` a letter at a time, then presses Enter when asked."""
        inside = self.glide(box, at=0.3)
        box.click(position={"x": inside[0], "y": inside[1]})
        box.fill("")
        self.frame(RING_FRAME_MS, 1.0)
        for letter in text:
            self.page.keyboard.type(letter)
            self.frame(KEY_FRAME_MS)
        if enter:
            time.sleep(0.2)
            box.press("Enter")
        self.hold(hold, busy_js)

    def key(self, key: str, *, hold: float = 1.2) -> None:
        """Presses a keyboard shortcut, showing it in a small key cap so the viewer knows what happened."""
        self.page.evaluate("""(k) => {
          let cap = document.getElementById('tour-key');
          if (!cap) { cap = document.createElement('div'); cap.id = 'tour-key'; document.body.append(cap); }
          cap.textContent = k;
          cap.style.cssText = 'position:fixed;right:24px;bottom:24px;z-index:100002;padding:8px 16px;border-radius:10px;'
            + 'font:600 22px/1 Inter,system-ui,sans-serif;background:#111827;color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.35);'
            + 'border:1px solid #4f46e5';
        }""", {"ArrowRight": "→", "ArrowLeft": "←"}.get(key, key))
        self.frame(RING_FRAME_MS * 3)
        self.page.keyboard.press(key)
        self.hold(0.5)
        self.page.evaluate("() => document.getElementById('tour-key')?.remove()")
        self.hold(hold)

    def choose(self, select: Any, value: str, *, hold: float = 1.2) -> None:
        """Glides to a drop-down and picks the option with this value."""
        self.glide(select, at=0.4)
        self.frame(RING_FRAME_MS, 0.8)
        select.select_option(value=value)
        self.frame(RING_FRAME_MS, 1.2)
        self.hold(hold)

    def scroll(self, pixels: int, *, steps: int = 20, within: str | None = None, hold: float = 1.0) -> None:
        """Scrolls the window (or the element matching `within`) smoothly by `pixels`."""
        for i in range(1, steps + 1):
            ease = (i / steps) ** 2 * (3 - 2 * i / steps) - ((i - 1) / steps) ** 2 * (3 - 2 * (i - 1) / steps)
            delta = pixels * ease
            if within:
                self.page.evaluate("([s, d]) => document.querySelector(s).scrollBy(0, d)", [within, delta])
            else:
                self.page.evaluate("(d) => window.scrollBy(0, d)", delta)
            self.frame(SCROLL_FRAME_MS)
        self.hold(hold)

    # -- output ---------------------------------------------------------------------------------------------------
    def save(self, path: Path) -> None:
        """
        Writes the frames as a looping, lossless animated WebP, and its last frame as a PNG beside it: the still for
        readers who prefer reduced motion, and the hero video's montage.
        """
        first, *others = [picture for picture, _ in self.frames]
        self.frames[-1][0].save(path.with_suffix(".png"), optimize=True)
        durations = [ms for _, ms in self.frames]
        first.save(path, "WEBP", save_all=True, append_images=others, loop=0, lossless=True, quality=100, method=6,
                   duration=durations, minimize_size=True)
        print(f"  {path.name}: {len(self.frames)} frames, {sum(durations) / 1000:.1f} s, "
              f"{path.stat().st_size / 1e6:.1f} MB", file=sys.stderr)


# ------------------------------------------------------------------------------------------------------- the tours

JOB_BUSY = ("[...document.querySelectorAll('#job-list .job')].some(j => /running|queued/i.test(j.textContent))"
            f" || {PAGE_BUSY}")


def play_upload(tour: Tour, app: DemoApp, ids: dict[str, str]) -> None:
    """Upload a scanned invoice, run Tesseract on it, and watch the job finish."""
    page = tour.page
    tour.goto("/", hold=1.4)
    drop = page.locator("#home-drop")
    tour.glide(drop, at=0.5)
    tour.frame(RING_FRAME_MS, 0.8)
    with page.expect_navigation(url="**/documents/*"):
        page.locator("#home-drop input[type=file]").set_input_files(str(app.docs["supplier-invoice-scan.pdf"]))
        tour.frame(RING_FRAME_MS, 1.2)
    page.add_style_tag(content=QUIET_CSS)
    tour.hold(1.4)
    tour.click(page.locator("#extract-form label:has(input[name=kind][value=ocr])"), hold=0.6)
    # pick the engine explicitly, so the tour is the same whichever engines this machine has installed
    tour.choose(page.locator("#ocr-engine"), "tesseract", hold=0.8)
    tour.click(page.locator("#extract-form button[type=submit]"), hold=0.2, busy_js="false")
    # the overview reloads itself ~1.5 s after the job ends: let that happen before clicking anything
    with page.expect_navigation(timeout=60_000):
        tour.wait(JOB_BUSY)
        tour.hold(1.6, JOB_BUSY)
    page.add_style_tag(content=QUIET_CSS)
    tour.hold(0.6)
    tour.click(page.locator(".doc-tabs a", has_text="Page inspector"), hold=0.8)
    tour.click(page.locator(".tabs button[data-tab=ocr]"), hold=2.6)



def play_inspector(tour: Tour, app: DemoApp, ids: dict[str, str]) -> None:
    """A scanned page: no embedded text, then its OCR and LLM text, all three side by side, and the next page."""
    page = tour.page
    tour.goto(f"/documents/{ids['notes']}/pages/1", hold=1.6)
    tour.click(page.locator(".tabs button[data-tab=ocr]"), hold=1.8)
    tour.click(page.locator(".tabs button[data-tab=llm]"), hold=1.8)
    tour.click(page.locator(".tabs button[data-tab=side]"), hold=2.4)
    tour.key("ArrowRight", hold=2.0)
    tour.click(page.locator(".tabs button[data-tab=compare]"), hold=0.6)
    tour.choose(page.locator("#cmp-ref"), _option(page, "#cmp-ref", "llm"), hold=0.4)
    tour.choose(page.locator("#cmp-hyp"), "ocr", hold=0.4)
    tour.click(page.locator("#run-compare"), hold=2.8)


def _option(page: Any, select: str, prefix: str) -> str:
    """The value of the first option of a drop-down starting with `prefix` (LLM sources are keyed by a hash)."""
    return page.evaluate("([s, p]) => [...document.querySelector(s).options].find(o => o.value.startsWith(p)).value",
                         [select, prefix])


def play_compare(tour: Tour, app: DemoApp, ids: dict[str, str]) -> None:
    """Score Tesseract against the LLM on the scan: document error rates, a bar per page, and a page's diff."""
    page = tour.page
    tour.goto(f"/documents/{ids['notes']}/compare", hold=1.2)
    tour.choose(page.locator("#src-a"), _option(page, "#src-a", "llm:"), hold=0.3)
    tour.choose(page.locator("#src-b"), "ocr", hold=0.3)
    tour.click(page.locator("#run"), hold=1.6)
    tour.click(page.locator("#cmp-bars .bar[data-goto]").first, hold=0.8)
    tour.scroll(430, hold=2.6)
    tour.click(page.locator("#pg-next"), hold=2.6)


def play_search(tour: Tour, app: DemoApp, ids: dict[str, str]) -> None:
    """Search every document and every source for a part number, then open a hit on its page."""
    page = tour.page
    tour.goto("/search", hold=0.8)
    tour.type(page.locator("#q"), "NB-0417", hold=1.8)
    tour.scroll(260, hold=1.4)
    tour.click(page.locator(".sr-page").first, hold=1.2)
    tour.click(page.locator(".tabs button[data-tab=ocr]"), hold=2.8)


def play_libraries(tour: Tour, app: DemoApp, ids: dict[str, str]) -> None:
    """A born-digital page read by every PDF library, one library's text, then PyMuPDF and pypdf compared."""
    page = tour.page
    # start from PyMuPDF whatever an earlier visit picked
    page.add_init_script("localStorage.removeItem('pyocr.nativeLibrary')")
    tour.goto(f"/documents/{ids['report']}/pages/1", hold=1.6)
    tour.click(page.locator("#check-libraries"), hold=2.8)
    tour.click(page.locator("#native-libraries [data-lib=pypdf]"), hold=2.2)
    tour.click(page.locator(".tabs button[data-tab=compare]"), hold=0.6)
    tour.choose(page.locator("#cmp-ref"), "native", hold=0.4)
    tour.choose(page.locator("#cmp-hyp"), "native:pypdf", hold=0.4)
    tour.click(page.locator("#run-compare"), hold=2.8)


def play_library(tour: Tour, app: DemoApp, ids: dict[str, str]) -> None:
    """The library, then a document's overview and its sources, exported all at once."""
    page = tour.page
    tour.goto("/documents", hold=2.0)
    tour.click(page.locator("a", has_text="field-notes-scan.pdf").first, hold=2.4)
    export_all = page.locator("summary", has_text="Export all")
    # bring the sources table and the button into view, leaving room below it for the opened menu
    top = page.evaluate("(el) => el.getBoundingClientRect().top", export_all.element_handle())
    tour.scroll(max(0, int(top - (VIEWPORT["height"] - 300))), hold=1.4)
    tour.click(export_all, hold=2.6)


@dataclass
class TourSpec:
    name: str
    play: Callable[[Tour, DemoApp, dict[str, str]], None]
    changes_data: bool = False


TOURS = [
    TourSpec("inspector", play_inspector),
    TourSpec("compare", play_compare),
    TourSpec("libraries", play_libraries),
    TourSpec("search", play_search),
    TourSpec("library", play_library),
    TourSpec("upload", play_upload, changes_data=True),
]


def record(app: DemoApp, ids: dict[str, str], spec: TourSpec, theme: str, out: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1, color_scheme=theme)
        context.add_init_script(f"localStorage.setItem('pyocr.theme', '{theme}')")
        page = context.new_page()
        tour = Tour(page, app.url)
        spec.play(tour, app, ids)
        tour.save(out / f"{spec.name}-{theme}.webp")
        browser.close()
    if spec.changes_data:  # undo the upload, so the next recording starts from the same library
        for doc in app_documents(app):
            if doc["id"] not in ids.values():
                _delete(app, doc["id"])


def app_documents(app: DemoApp) -> list[dict]:
    from demo_data import _call

    return _call(app.url, "GET", "/api/documents")


def _delete(app: DemoApp, doc_id: str) -> None:
    from demo_data import _call

    _call(app.url, "DELETE", f"/api/documents/{doc_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tours", nargs="*", help="tour names (default: all)")
    parser.add_argument("--theme", choices=THEMES, help="one theme only (default: both)")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for spec in TOURS:
            print(f"{spec.name:10} {spec.play.__doc__}")
        return 0
    unknown = set(args.tours) - {t.name for t in TOURS}
    if unknown:
        parser.error(f"unknown tour(s): {', '.join(sorted(unknown))}")
    chosen = [t for t in TOURS if not args.tours or t.name in args.tours]
    themes = (args.theme,) if args.theme else THEMES
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyocr-tours-") as tmp:
        app = DemoApp(Path(tmp))
        try:
            print("seeding the demo app ...", file=sys.stderr)
            ids = app.seed()
            for spec in chosen:
                for theme in themes:
                    print(f"{spec.name} ({theme})", file=sys.stderr)
                    record(app, ids, spec, theme, args.out)
        finally:
            app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
