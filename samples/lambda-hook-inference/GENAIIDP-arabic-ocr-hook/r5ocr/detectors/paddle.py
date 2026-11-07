"""Arabic-strong line geometry from PaddleOCR PP-OCRv5 (mobile det + Arabic rec)."""

import io
import threading

import numpy as np
from PIL import Image

from ..geometry import Box

_ENGINE = None
_LOCK = threading.Lock()


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        with _LOCK:
            if _ENGINE is None:
                from paddleocr import (
                    PaddleOCR,
                )  # heavy import, deferred past Lambda init

                _ENGINE = PaddleOCR(
                    lang="ar",
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="arabic_PP-OCRv5_mobile_rec",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    device="cpu",
                    text_det_limit_type="max",
                    text_det_limit_side_len=2048,
                    # oneDNN's PIR-based CPU executor (paddlepaddle 3.3.1) raises
                    # "NotImplementedError: ConvertPirAttribute2RuntimeAttribute
                    # not support [pir::ArrayAttribute<pir::DoubleAttribute>]" on
                    # the detector's padding op on CPUs whose oneDNN kernel
                    # selection differs from the dev/CI machine (observed under
                    # QEMU x86_64 emulation on arm64; Lambda's own x86_64 CPU
                    # fleet is not guaranteed uniform either). Plain CPU inference
                    # without oneDNN is slightly slower but correct everywhere.
                    enable_mkldnn=False,
                )
    return _ENGINE


def detect(image_bytes: bytes, engine: object | None = None) -> list[Box]:
    engine = engine or get_engine()
    with Image.open(io.BytesIO(image_bytes)) as im:
        arr = np.asarray(im.convert("RGB"))[:, :, ::-1].copy()  # PaddleOCR expects BGR
    out: list[Box] = []
    for res in engine.predict(arr):
        d = res.json.get("res", res.json)
        for poly, text in zip(d.get("rec_polys", []), d.get("rec_texts", [])):
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            out.append(
                Box(
                    float(min(xs)),
                    float(min(ys)),
                    float(max(xs)),
                    float(max(ys)),
                    "paddle",
                    text or "",
                )
            )
    return out
