"""Image preprocessing applied to a page before it is sent to an LLM.

Why this exists: a vision model reads pixels, so the pixels matter. A faded
scan, a grey photocopy or a skewed phone photo can be transcribed worse than
the same page after grayscale + auto-levels, or better *without* them. The
only way to know is to try both and compare, so preprocessing is a first-class
part of an LLM run rather than something users do in another tool.

Design choices:

* **One canonical text form** (``"gray;auto;contrast=1.5"``) is the wire format
  for the API, the CLI flag, the job body, the stored record *and* the cache
  key. Parsing normalises it (fixed step order, defaults dropped, numbers
  rounded), so two ways of writing the same settings share one cached result.
* **Fixed pipeline order**, whatever order the user wrote the steps in:
  rotate → crop → grayscale → denoise → auto-levels / equalize → brightness →
  contrast → sharpness → threshold → invert. Geometry first (so crop edges
  refer to the page as you see it after rotating), denoise before contrast
  (contrast would amplify the noise), binarize near the end (it throws away
  information every later step would want).
* **The empty pipeline is the identity** and never touches Pillow, so runs
  without preprocessing send exactly the bytes they always did and keep their
  existing cache keys.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, fields, replace
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

#: Above this size a PNG is re-encoded as JPEG to stay inside provider upload limits.
MAX_PNG_BYTES = 3_500_000

ROTATIONS = (0, 90, 180, 270)
DENOISE_SIZES = (0, 3, 5)


@dataclass(frozen=True)
class Preprocess:
    """A normalised preprocessing pipeline. Build it with :meth:`parse`."""

    rotate: int = 0                     # clockwise degrees: 0, 90, 180, 270
    crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # % trimmed off top, right, bottom, left
    dpi: int | None = None              # render resolution; None = LLM_DPI
    gray: bool = False
    denoise: int = 0                    # median filter size: 0 (off), 3, 5
    auto: bool = False                  # stretch levels so the darkest ink is black and the paper white
    equalize: bool = False              # histogram equalisation (brings out faint detail)
    brightness: float = 1.0
    contrast: float = 1.0
    sharpness: float = 1.0              # <1 softens, >1 sharpens
    threshold: int | str | None = None  # binarize: "otsu" or a fixed level 1-254
    invert: bool = False

    # ------------------------------------------------------------------ parsing
    @classmethod
    def parse(cls, spec: str | None) -> Preprocess:
        """Parse ``"gray;contrast=1.5"`` (or a preset id such as ``"clean"``). Raises ``ValueError``."""
        spec = (spec or "").strip()
        if spec in PRESETS:
            spec = PRESETS[spec]["spec"]
        values: dict[str, Any] = {}
        for token in spec.split(";"):
            token = token.strip()
            if not token:
                continue
            name, _, raw = token.partition("=")
            name, raw = name.strip().lower(), raw.strip()
            if name not in _PARSERS:
                known = ", ".join(sorted(_PARSERS))
                raise ValueError(f"unknown preprocessing step '{name}' (known steps: {known}; presets: {', '.join(PRESETS)})")
            try:
                values[name] = _PARSERS[name](raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"bad value for '{name}': {exc}") from exc
        pre = cls(**values)
        if pre.threshold is not None and not pre.gray:
            pre = replace(pre, gray=True)  # binarizing implies grayscale; one spelling, one cache key
        return pre

    def __str__(self) -> str:
        """Canonical form: steps in pipeline order, defaults omitted. ``""`` means "no preprocessing"."""
        parts = []
        if self.rotate:
            parts.append(f"rotate={self.rotate}")
        if any(self.crop):
            parts.append("crop=" + ",".join(_num(v) for v in self.crop))
        if self.dpi:
            parts.append(f"dpi={self.dpi}")
        if self.gray:
            parts.append("gray")
        if self.denoise:
            parts.append(f"denoise={self.denoise}")
        if self.auto:
            parts.append("auto")
        if self.equalize:
            parts.append("equalize")
        for name in ("brightness", "contrast", "sharpness"):
            v = getattr(self, name)
            if v != 1.0:
                parts.append(f"{name}={_num(v)}")
        if self.threshold is not None:
            parts.append(f"threshold={self.threshold}")
        if self.invert:
            parts.append("invert")
        return ";".join(parts)

    @property
    def is_identity(self) -> bool:
        return not str(self)

    @property
    def changes_pixels(self) -> bool:
        """True if anything beyond the render resolution changes (i.e. Pillow has work to do)."""
        return not replace(self, dpi=None).is_identity

    def describe(self) -> str:
        """Short human label, e.g. ``"grayscale, contrast ×1.5"`` (``"original image"`` if empty)."""
        bits = []
        if self.rotate:
            bits.append(f"rotated {self.rotate}°")
        if any(self.crop):
            bits.append("cropped")
        if self.dpi:
            bits.append(f"{self.dpi} dpi")
        if self.threshold is not None:
            bits.append("black & white" + (" (Otsu)" if self.threshold == "otsu" else f" @{self.threshold}"))
        elif self.gray:
            bits.append("grayscale")
        if self.denoise:
            bits.append("denoised")
        if self.auto:
            bits.append("auto levels")
        if self.equalize:
            bits.append("equalized")
        for name in ("brightness", "contrast", "sharpness"):
            v = getattr(self, name)
            if v != 1.0:
                bits.append(f"{name} ×{_num(v)}")
        if self.invert:
            bits.append("inverted")
        return ", ".join(bits) or "original image"

    # ------------------------------------------------------------------ pixels
    def apply(self, img: Image.Image) -> Image.Image:
        """Run the pipeline on a Pillow image (the render resolution is handled by the caller)."""
        if self.rotate:
            img = img.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_90}[self.rotate])
        if any(self.crop):
            top, right, bottom, left = self.crop
            w, h = img.size
            box = (round(w * left / 100), round(h * top / 100), round(w * (1 - right / 100)), round(h * (1 - bottom / 100)))
            img = img.crop(box)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if self.gray:
            img = img.convert("L")
        if self.denoise:
            img = img.filter(ImageFilter.MedianFilter(self.denoise))
        if self.auto:
            img = ImageOps.autocontrast(img, cutoff=1)
        if self.equalize:
            img = ImageOps.equalize(img)
        if self.brightness != 1.0:
            img = ImageEnhance.Brightness(img).enhance(self.brightness)
        if self.contrast != 1.0:
            img = ImageEnhance.Contrast(img).enhance(self.contrast)
        if self.sharpness != 1.0:
            img = ImageEnhance.Sharpness(img).enhance(self.sharpness)
        if self.threshold is not None:
            level = otsu_level(img) if self.threshold == "otsu" else int(self.threshold)
            img = img.point(lambda v: 255 if v > level else 0)
        if self.invert:
            img = ImageOps.invert(img)
        return img

    def to_json(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["crop"] = list(self.crop)
        out["spec"] = str(self)
        out["label"] = self.describe()
        return out


def otsu_level(img: Image.Image) -> int:
    """Otsu's threshold: the grey level that best splits the histogram into "ink" and "paper".

    For every candidate level t it measures how far apart the mean brightness of
    the two classes is, weighted by their sizes (between-class variance), and
    keeps the t where that separation is largest. No tuning knob, which is why
    it's the default for black & white.
    """
    hist = img.convert("L").histogram()
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    weight_bg = sum_bg = 0
    best, level = -1.0, 127
    for t in range(256):
        weight_bg += hist[t]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += t * hist[t]
        mean_bg, mean_fg = sum_bg / weight_bg, (sum_all - sum_bg) / weight_fg
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if between > best:
            best, level = between, t
    return level


def encode(img: Image.Image) -> tuple[bytes, str]:
    """PNG (crisp text edges), or JPEG when the PNG would be too large to upload."""
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    if buf.tell() <= MAX_PNG_BYTES:
        return buf.getvalue(), "image/png"
    buf = io.BytesIO()
    img.convert("L" if img.mode in ("L", "1") else "RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


def process_bytes(data: bytes, pre: Preprocess) -> tuple[bytes, str]:
    """Decode a rendered page (any format Pillow reads), apply ``pre``, re-encode."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        return encode(pre.apply(img))


# ---------------------------------------------------------------------- parsing helpers
def _num(v: float) -> str:
    return f"{round(v, 2):g}"


def _flag(raw: str) -> bool:
    if raw.lower() in ("", "1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected on/off, got '{raw}'")


def _ranged(lo: float, hi: float):
    def parse(raw: str) -> float:
        v = round(float(raw), 2)
        if not lo <= v <= hi:
            raise ValueError(f"{v} is outside {lo}-{hi} (1 = unchanged)")
        return v
    return parse


def _rotate(raw: str) -> int:
    v = int(raw) % 360
    if v not in ROTATIONS:
        raise ValueError("use 90, 180 or 270")
    return v


def _crop(raw: str) -> tuple[float, float, float, float]:
    parts = [round(float(p), 1) for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("give four percentages: top,right,bottom,left")
    if any(not 0 <= p <= 45 for p in parts):
        raise ValueError("each edge can trim 0-45%")
    top, right, bottom, left = parts
    if top + bottom > 80 or left + right > 80:
        raise ValueError("crop would leave less than 20% of the page")
    return (top, right, bottom, left)


def _dpi(raw: str) -> int | None:
    v = int(raw)
    if not 72 <= v <= 600:
        raise ValueError("dpi must be 72-600")
    return v


def _denoise(raw: str) -> int:
    v = int(raw or 3)
    if v not in DENOISE_SIZES:
        raise ValueError("use 3 (light) or 5 (strong)")
    return v


def _threshold(raw: str) -> int | str | None:
    if raw.lower() in ("", "otsu", "auto"):
        return "otsu"
    v = int(raw)
    if not 1 <= v <= 254:
        raise ValueError("use 'otsu' or a level 1-254")
    return v


_PARSERS = {
    "rotate": _rotate,
    "crop": _crop,
    "dpi": _dpi,
    "gray": _flag,
    "denoise": _denoise,
    "auto": _flag,
    "equalize": _flag,
    "brightness": _ranged(0.3, 2.5),
    "contrast": _ranged(0.3, 3.0),
    "sharpness": _ranged(0.0, 4.0),
    "threshold": _threshold,
    "invert": _flag,
}

#: Named starting points, shown as a filter strip in the UI and accepted by ``--preprocess``.
PRESETS: dict[str, dict[str, str]] = {
    "original": {"label": "Original", "spec": "", "hint": "The page exactly as rendered"},
    "clean": {"label": "Clean scan", "spec": "gray;auto;sharpness=1.5",
              "hint": "Grayscale, stretch levels, a little sharpening: a safe first try for scans"},
    "contrast": {"label": "High contrast", "spec": "gray;contrast=1.8", "hint": "Darker ink, whiter paper"},
    "bw": {"label": "Black & white", "spec": "gray;threshold=otsu",
           "hint": "Binarize with Otsu's method; strong, can erase faint strokes"},
    "faded": {"label": "Fix faded", "spec": "auto;brightness=1.05;contrast=1.4;sharpness=2",
              "hint": "Old or washed-out prints and photocopies"},
    "denoise": {"label": "Denoise", "spec": "gray;denoise=3;sharpness=1.5",
                "hint": "Speckled faxes and noisy phone photos"},
    "detail": {"label": "Bring out detail", "spec": "gray;equalize", "hint": "Histogram equalisation for very faint text"},
    "invert": {"label": "Invert", "spec": "gray;invert", "hint": "Light text on a dark background (screenshots, negatives)"},
}


def presets_json() -> list[dict[str, str]]:
    return [{"id": k, **v} for k, v in PRESETS.items()]
