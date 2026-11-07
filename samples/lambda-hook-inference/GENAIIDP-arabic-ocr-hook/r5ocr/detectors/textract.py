"""English-line geometry from Amazon Textract DetectDocumentText.

Geometry is the primary product; each segment
also carries the detector's own word text, which M0 (settings.READ_METHOD)
uses directly with no separate reader call.
"""

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ..geometry import Box, union

_THROTTLE = {
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "LimitExceededException",
    "TooManyRequestsException",
}


class TextractUnavailable(RuntimeError):
    """Textract could not be used for this page (throttled after retries or error)."""


# botocore's Config `max_attempts` counts RETRIES (total = max_attempts + 1), so
# the total attempt count is set with `total_max_attempts` to keep the budget exact.
# Worst case per call is ~40 s (30 s read + connect), so at most 3 attempts plus
# backoff stays inside the handler's 130 s Textract budget (DETECTOR_BUDGET_S).
MAX_ATTEMPTS_CAP = 3


def make_client(max_attempts: int):
    return boto3.client(
        "textract",
        config=Config(
            connect_timeout=10,
            read_timeout=30,
            retries={
                "mode": "adaptive",
                "total_max_attempts": max(1, min(max_attempts, MAX_ATTEMPTS_CAP)),
            },
        ),
    )


def split_line_on_gaps(
    line_box: Box, word_boxes: list[Box], gap_factor: float = 1.5
) -> list[Box]:
    """Split a LINE into segments on large horizontal gaps between words.

    Each returned segment's text is its words' text joined with a single
    space, in ascending x0 order. If the LINE has no WORD children,
    ``line_box`` (which already carries the LINE's own text) is returned
    unchanged.
    """
    if not word_boxes:
        return [line_box]
    words = sorted(word_boxes, key=lambda b: b.x0)
    limit = gap_factor * max(line_box.height(), 1.0)
    segments = []
    current = [words[0]]
    for w in words[1:]:
        if w.x0 - max(b.x1 for b in current) > limit:
            segments.append(current)
            current = [w]
        else:
            current.append(w)
    segments.append(current)
    out = []
    for seg in segments:
        u = union(seg, "textract")
        text = " ".join(w.text for w in seg)
        out.append(Box(u.x0, u.y0, u.x1, u.y1, "textract", text))
    return out


def _px(bb: dict, width: int, height: int, source: str, text: str = "") -> Box:
    return Box(
        bb["Left"] * width,
        bb["Top"] * height,
        (bb["Left"] + bb["Width"]) * width,
        (bb["Top"] + bb["Height"]) * height,
        source,
        text,
    )


def detect(client, image_bytes: bytes, width: int, height: int) -> list[Box]:
    try:
        response = client.detect_document_text(Document={"Bytes": image_bytes})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = code or "ClientError"
        raise TextractUnavailable(message) from exc
    except BotoCoreError as exc:
        raise TextractUnavailable(type(exc).__name__) from exc
    blocks = response["Blocks"]
    by_id = {b["Id"]: b for b in blocks}
    out: list[Box] = []
    for b in blocks:
        if b.get("BlockType") != "LINE":
            continue
        line = _px(
            b["Geometry"]["BoundingBox"], width, height, "textract", b.get("Text", "")
        )
        child_ids = [
            i
            for r in b.get("Relationships", [])
            if r.get("Type") == "CHILD"
            for i in r.get("Ids", [])
        ]
        words = [
            _px(
                by_id[i]["Geometry"]["BoundingBox"],
                width,
                height,
                "textract",
                by_id[i].get("Text", ""),
            )
            for i in child_ids
            if i in by_id and by_id[i].get("BlockType") == "WORD"
        ]
        out.extend(split_line_on_gaps(line, words))
    return out
