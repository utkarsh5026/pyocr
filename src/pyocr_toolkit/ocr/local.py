"""Local deep-learning OCR engines: RapidOCR, EasyOCR, PaddleOCR and docTR.

They run entirely on this machine (nothing is uploaded) but need extra Python
packages, installed with an extra such as ``pip install "pyocr-toolkit[easyocr]"``.
Each library downloads its model weights the first time it runs, so the first
page is slow and needs internet access once; later runs are offline.

Models are loaded lazily on first use and kept in memory. Calls are
serialised per engine (``self._lock``) because none of these libraries
promise thread safety.

The libraries changed their result formats between major versions
(PaddleOCR 2 -> 3, rapidocr_onnxruntime -> rapidocr 2/3), so the parsers
below accept both shapes.
"""

from __future__ import annotations

from typing import Any

from ..documents import RenderedImage
from .base import Box, OCREngine, OCRResult, rect_of, words_from_boxes


def _to_list(x: Any) -> list:
    """numpy array / tuple / None -> plain list."""
    if x is None:
        return []
    return x.tolist() if hasattr(x, "tolist") else list(x)


def _rgb_array(image: RenderedImage):
    import numpy as np

    return np.frombuffer(image.data, dtype=np.uint8).reshape(image.height_px, image.width_px, 3)


class RapidOCREngine(OCREngine):
    id = "rapidocr"
    label = "RapidOCR"
    brand = "rapidocr"
    description = ("PaddleOCR's models on ONNX Runtime: light install (no PyTorch / Paddle), "
                   "good on printed English and Chinese.")
    docs_url = "https://rapidai.github.io/RapidOCRDocs/"
    requires = [("rapidocr", "rapidocr_onnxruntime")]
    pip_extra = "rapidocr"

    def _load(self):
        try:
            from rapidocr import RapidOCR
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()

    def recognize(self, image: RenderedImage) -> OCRResult:
        with self._lock:
            engine = self._with_model(self._load)
            out = engine(image.data)
        return OCRResult(words=words_from_boxes(parse_rapidocr(out)))


def parse_rapidocr(out: Any) -> list[Box]:
    # rapidocr >= 2: an object with .boxes / .txts; rapidocr_onnxruntime: (result, elapse)
    # where result is [[box, text, score], ...] or None.
    if hasattr(out, "txts"):
        triples = zip(_to_list(out.boxes), _to_list(out.txts))
    else:
        result = out[0] if isinstance(out, tuple) else out
        triples = ((r[0], r[1]) for r in (result or []))
    return [(*rect_of(box), str(text)) for box, text in triples]


class EasyOCREngine(OCREngine):
    id = "easyocr"
    label = "EasyOCR"
    brand = "easyocr"
    description = "PyTorch-based OCR for 80+ languages. Solid on photos and mixed scripts; heavier install (PyTorch)."
    docs_url = "https://github.com/JaidedAI/EasyOCR"
    env_vars = [("EASYOCR_LANGUAGES", False, "Comma-separated language codes, e.g. en,de"),
                ("OCR_GPU", False, "Use a CUDA GPU (true/false)")]
    requires = [("easyocr",)]
    pip_extra = "easyocr"

    def languages(self) -> list[str]:
        return [x.strip() for x in self.settings.easyocr_languages.split(",") if x.strip()] or ["en"]

    def settings_summary(self) -> str:
        return f"lang {','.join(self.languages())}" + (" · GPU" if self.settings.ocr_gpu else "")

    def recognize(self, image: RenderedImage) -> OCRResult:
        def load():
            import easyocr
            return easyocr.Reader(self.languages(), gpu=self.settings.ocr_gpu, verbose=False)

        with self._lock:
            reader = self._with_model(load)
            result = reader.readtext(image.data, detail=1, paragraph=False)
        boxes = [(*rect_of(box), str(text)) for box, text, *_ in result]
        return OCRResult(words=words_from_boxes(boxes))


class PaddleOCREngine(OCREngine):
    id = "paddleocr"
    label = "PaddleOCR"
    brand = "paddleocr"
    description = "Baidu's PP-OCR pipeline. Very accurate on dense and multilingual pages; heavy install (PaddlePaddle)."
    docs_url = "https://paddlepaddle.github.io/PaddleOCR/"
    env_vars = [("PADDLEOCR_LANGUAGE", False, "Language code, e.g. en, ch, german, japan")]
    requires = [("paddleocr",), ("paddle",)]
    pip_extra = "paddleocr"
    image_format = "raw"

    def settings_summary(self) -> str:
        return f"lang {self.settings.paddleocr_language}"

    def _load(self):
        from paddleocr import PaddleOCR

        lang = self.settings.paddleocr_language
        try:  # PaddleOCR 3.x: skip the page-level unwarping/orientation models
            return PaddleOCR(lang=lang, use_doc_orientation_classify=False, use_doc_unwarping=False,
                             use_textline_orientation=False)
        except (TypeError, ValueError):  # PaddleOCR 2.x
            return PaddleOCR(lang=lang, use_angle_cls=False, show_log=False)

    def recognize(self, image: RenderedImage) -> OCRResult:
        bgr = _rgb_array(image)[:, :, ::-1].copy()  # Paddle expects OpenCV's BGR order
        with self._lock:
            engine = self._with_model(self._load)
            out = engine.predict(bgr) if hasattr(engine, "predict") else engine.ocr(bgr, cls=False)
        return OCRResult(words=words_from_boxes(parse_paddleocr(out)))


def parse_paddleocr(out: Any) -> list[Box]:
    boxes: list[Box] = []
    for res in out or []:
        if res is None:
            continue
        if hasattr(res, "keys") and "rec_texts" in res.keys():  # 3.x result object / dict
            polys = res["rec_polys"] if "rec_polys" in res.keys() else res.get("dt_polys", [])
            boxes += [(*rect_of(_to_list(p)), str(t)) for p, t in zip(_to_list(polys), res["rec_texts"])]
        else:  # 2.x: [[box, (text, score)], ...]
            boxes += [(*rect_of(line[0]), str(line[1][0])) for line in res]
    return boxes


class DocTREngine(OCREngine):
    id = "doctr"
    label = "docTR"
    brand = "doctr"
    description = ("Mindee's document OCR (detection + recognition models on PyTorch). "
                   "Returns blocks, lines and words; strong on Latin-script documents.")
    docs_url = "https://mindee.github.io/doctr/"
    env_vars = [("OCR_GPU", False, "Use a CUDA GPU (true/false)")]
    requires = [("doctr",)]
    pip_extra = "doctr"

    def _load(self):
        from doctr.models import ocr_predictor

        model = ocr_predictor(pretrained=True)
        if self.settings.ocr_gpu:
            model = model.cuda()
        return model

    def recognize(self, image: RenderedImage) -> OCRResult:
        from doctr.io import DocumentFile

        with self._lock:
            model = self._with_model(self._load)
            result = model(DocumentFile.from_images([image.data]))
        return OCRResult(words=parse_doctr(result, image.width_px, image.height_px))


def parse_doctr(result: Any, width: int, height: int):
    """docTR geometry is relative (0..1), either ((x0, y0), (x1, y1)) or a 4-point polygon."""
    from ..documents import Word

    words = []
    line_no = 0
    page = result.pages[0]
    for b, block in enumerate(page.blocks):
        for line in block.lines:
            for w in line.words:
                x0, y0, x1, y1 = rect_of(w.geometry)
                words.append(Word(x0 * width, y0 * height, x1 * width, y1 * height, w.value, b, line_no))
            line_no += 1
    return words
