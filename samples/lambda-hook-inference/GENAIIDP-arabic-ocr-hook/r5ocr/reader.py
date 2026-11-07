"""M1-M3 only: the reader model assigns verbatim text to each detector region."""

import io
import json
import re
import time

from PIL import Image, ImageDraw, ImageFont

from .llm import Usage, converse
from .payload import HookRequest
from .regions import Region
from .settings import Settings

SYSTEM = (
    "You transcribe the text inside marked regions of a document image exactly as "
    "printed. Arabic stays in Arabic script; never translate or transliterate. "
    "Output only JSON."
)
_TASK = (
    'Return {{"lines":[{{"id":<int>,"text":"<verbatim text, or empty string if the '
    'region has no text>"}}]}} with one entry for EVERY region id: {ids}. {extra}'
)
_COLOURS = ["#e11d48", "#2563eb", "#16a34a", "#c026d3"]

# Bedrock Converse per-image limits: 3.75 MiB raw bytes, 8000px per side.
MAX_IMAGE_BYTES = 3_932_160
MAX_SIDE = 8000

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*(.*?)\s*```\s*$", re.S)


class ReaderFormatError(ValueError):
    pass


class ReaderDeadlineExceeded(TimeoutError):
    """Not enough time budget remains to safely start another reader Converse
    call before the Lambda deadline."""


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def _lines_from(obj) -> list | None:
    if isinstance(obj, dict) and isinstance(obj.get("lines"), list):
        return obj["lines"]
    return None


def parse_reply(text: str, valid_ids: set[int]) -> dict[int, str]:
    stripped = _strip_fences(text)
    lines = None
    try:
        lines = _lines_from(json.loads(stripped))
    except json.JSONDecodeError:
        pass
    if lines is None:
        decoder = json.JSONDecoder()
        for i, ch in enumerate(stripped):
            if ch != "{":
                continue
            try:
                obj, _end = decoder.raw_decode(stripped, i)
            except json.JSONDecodeError:
                continue
            lines = _lines_from(obj)
            if lines is not None:
                break
    if lines is None:
        raise ReaderFormatError("no JSON object with a 'lines' list in reader reply")
    out = {}
    for item in lines:
        try:
            rid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if rid in valid_ids:
            out[rid] = str(item.get("text") or "").strip()
    return out


def _downscale_to_max_side(img: Image.Image) -> Image.Image:
    longest = max(img.width, img.height)
    if longest <= MAX_SIDE:
        return img
    scale = MAX_SIDE / longest
    size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    return img.resize(size)


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _fit_within_limits(img: Image.Image, preferred: str) -> tuple[bytes, str]:
    """Encode img so it satisfies Bedrock's per-image size/side limits.

    Tries `preferred` ("png" or "jpeg") first; if PNG is too large, or JPEG at
    quality 90/80/70 is still too large, downscales by 0.85 repeatedly
    (re-encoding as JPEG at quality 80) until it fits.
    """
    img = _downscale_to_max_side(img)
    if preferred == "png":
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
        if len(data) <= MAX_IMAGE_BYTES:
            return data, "png"
    data = b""
    for quality in (90, 80, 70):
        data = _encode_jpeg(img, quality)
        if len(data) <= MAX_IMAGE_BYTES:
            return data, "jpeg"
    while len(data) > MAX_IMAGE_BYTES:
        img = img.resize(
            (max(1, int(img.width * 0.85)), max(1, int(img.height * 0.85)))
        )
        data = _encode_jpeg(img, 80)
    return data, "jpeg"


def _font_for_size(size: int):
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _label_position(
    extent, tw: float, th: float, width: int, height: int
) -> tuple[float, float]:
    """Choose a label rectangle that avoids covering the region's own text.

    Order: (a) above-left, outside the box; (b) left of the box, vertically
    centred, if there is room on the left; (c) right of the box, vertically
    centred, if there is room on the right; (d) last resort, clipped to the
    image at (0, 0)-relative offsets so it never reports negative coordinates.
    """
    x0, y0, x1, y1 = extent.x0, extent.y0, extent.x1, extent.y1
    if x0 - tw >= 0:
        if y0 - th >= 0:
            return x0 - tw, y0 - th
        mid_y = min(max(0.0, (y0 + y1) / 2 - th / 2), max(0.0, height - th))
        return x0 - tw, mid_y
    if x1 + tw <= width:
        mid_y = min(max(0.0, (y0 + y1) / 2 - th / 2), max(0.0, height - th))
        return x1, mid_y
    return max(0.0, x0 - tw), max(0.0, y0 - th)


def _overlay(req: HookRequest, regions: list[Region]) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(req.image_bytes)).convert("RGB")
    d = ImageDraw.Draw(img)
    base = int(min(req.width, req.height) / 80)
    for r in regions:
        c = _COLOURS[r.id % len(_COLOURS)]
        e = r.extent
        d.rectangle([e.x0, e.y0, e.x1, e.y1], outline=c, width=2)
        size = max(10, min(int(0.8 * e.height()), base))
        font = _font_for_size(size)
        label = str(r.id)
        tw = d.textlength(label, font=font) + 6
        th = size + 4
        lx, ly = _label_position(e, tw, th, img.width, img.height)
        d.rectangle([lx, ly, lx + tw, ly + th], fill=c)
        d.text((lx + 3, ly + 1), label, fill="white", font=font)
    # Drawing happens on the full-resolution image; sizing/downscale happens
    # only afterward, so region boxes stay pixel-aligned to req.width/height.
    return _fit_within_limits(img, "jpeg")


def _check_deadline(deadline: float | None, min_call_budget_s: float) -> None:
    if deadline is not None and (deadline - time.monotonic()) < min_call_budget_s:
        raise ReaderDeadlineExceeded(
            "not enough time budget left to start another reader call"
        )


def _call(
    client,
    settings,
    content,
    ids,
    *,
    deadline: float | None = None,
    min_call_budget_s: float = 0.0,
) -> tuple[dict[int, str], Usage]:
    usage = Usage()
    for attempt in range(2):
        _check_deadline(deadline, min_call_budget_s)
        text, u = converse(
            client,
            settings.reader_model_id,
            SYSTEM,
            content,
            settings.reader_effort,
            settings.max_output_tokens,
        )
        usage = usage + u
        try:
            return parse_reply(text, set(ids)), usage
        except ReaderFormatError:
            if attempt == 1:
                raise
    raise ReaderFormatError("unreachable")


def read_regions(
    client,
    settings: Settings,
    req: HookRequest,
    regions: list[Region],
    *,
    deadline: float | None = None,
    min_call_budget_s: float = 0.0,
) -> tuple[dict[int, str], Usage]:
    ids = [r.id for r in regions]
    if not ids:
        return {}, Usage()
    if settings.read_method == "M1":
        data, fmt = _overlay(req, regions)
        content = [
            {
                "text": _TASK.format(
                    ids=ids, extra="Regions are outlined and numbered on the image."
                )
            },
            {"image": {"format": fmt, "source": {"bytes": data}}},
        ]
        return _call(
            client,
            settings,
            content,
            ids,
            deadline=deadline,
            min_call_budget_s=min_call_budget_s,
        )
    if settings.read_method == "M2":
        boxes = [
            {
                "id": r.id,
                "box": [
                    round(1000 * r.extent.x0 / req.width),
                    round(1000 * r.extent.y0 / req.height),
                    round(1000 * r.extent.x1 / req.width),
                    round(1000 * r.extent.y1 / req.height),
                ],
            }
            for r in regions
        ]
        content = [
            {
                "text": _TASK.format(
                    ids=ids,
                    extra="Region boxes are [left, top, right, bottom] on a "
                    "0-1000 grid: " + json.dumps(boxes),
                )
            },
            {
                "image": {
                    "format": req.image_format,
                    "source": {"bytes": req.image_bytes},
                }
            },
        ]
        return _call(
            client,
            settings,
            content,
            ids,
            deadline=deadline,
            min_call_budget_s=min_call_budget_s,
        )
    img = Image.open(io.BytesIO(req.image_bytes)).convert("RGB")
    texts: dict[int, str] = {}
    usage = Usage()
    for start in range(0, len(regions), 8):
        batch = regions[start : start + 8]
        content: list[dict] = []
        for r in batch:
            e = r.extent
            pad = max(4, 0.15 * e.height())
            crop = img.crop(
                (
                    max(0, e.x0 - pad),
                    max(0, e.y0 - pad),
                    min(img.width, e.x1 + pad),
                    min(img.height, e.y1 + pad),
                )
            )
            if crop.height < 48:
                crop = crop.resize(
                    (max(1, int(crop.width * 48 / max(crop.height, 1))), 48)
                )
            data, fmt = _fit_within_limits(crop, "png")
            content += [
                {"text": f"Region {r.id}:"},
                {"image": {"format": fmt, "source": {"bytes": data}}},
            ]
        content.insert(
            0,
            {
                "text": _TASK.format(
                    ids=[r.id for r in batch],
                    extra="Each region is a separate image.",
                )
            },
        )
        got, u = _call(
            client,
            settings,
            content,
            [r.id for r in batch],
            deadline=deadline,
            min_call_budget_s=min_call_budget_s,
        )
        texts.update(got)
        usage = usage + u
    return texts, usage
