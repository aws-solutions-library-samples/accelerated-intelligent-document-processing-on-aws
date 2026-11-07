"""Orchestration. Invariant: the page text always equals the IDP's own full-page
Bedrock transcription; boxes are purely additive, and any failure in the box
pipeline returns that text with no boxes.
"""

import logging
import re
import time
from typing import Callable

import boto3
from botocore.config import Config

from . import metrics
from .blocks import to_textract_blocks
from .detectors import paddle as paddle_det
from .detectors import textract as textract_det
from .detectors.textract import TextractUnavailable
from .geometry import Box, vertical_overlap_ratio
from .page_text import transcribe_page
from .payload import HookRequest, parse_request
from .reader import read_regions
from .regions import Region, build_regions
from .routing import choose_box
from .script import classify
from .settings import Settings
from .snap import snap_to_transcription

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_CLIENTS: dict[str, object] = {}

# A word with at least one Latin letter or Western digit.
_LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9]")

# Order in which detectors run. "textract" is called with (img, width, height);
# "paddle" is called with (img,) only.
_DETECTOR_ORDER = ("paddle", "textract")

# Worst-case seconds each detector may need. A detector is skipped
# (DetectorDegraded reason=DeadlineSkip) when less than this remains before the
# deadline, so a slow transcription call can never push a detector past the Lambda timeout.
# Textract: 3 attempts x ~40 s (30 s read + connect) + backoff ~= 130 s.
DETECTOR_BUDGET_S = {"paddle": 60.0, "textract": 130.0}

# The full-page transcription (up to max_output_tokens) can legitimately run
# long; the IDP's own Bedrock client uses read_timeout=300 for the same reason
# (see lib/idp_common_pkg/idp_common/bedrock/client.py) so botocore's 60s
# default does not fail a call the plain Bedrock OCR path would have completed.
# total_max_attempts=1 (one attempt, no botocore retries), as the IDP's own
# client does: the IDP already retries a failed hook invocation, and stacked
# botocore retries (4 x 300s) could otherwise outlive the 780s Lambda timeout.
_TRANSCRIBE_CONFIG = Config(
    connect_timeout=10,
    read_timeout=300,
    retries={"mode": "standard", "total_max_attempts": 1},
)

# The reader (region-text) Converse calls are budgeted separately and more
# tightly, since read_regions itself enforces a time budget and a slow
# reader call must not be allowed to consume the whole remaining deadline with
# botocore-level retries. One attempt, so READER_READ_TIMEOUT_S + connect time
# is the true worst case that read_regions' per-call budget reserves.
READER_READ_TIMEOUT_S = 120
_READER_CONFIG = Config(
    connect_timeout=10,
    read_timeout=READER_READ_TIMEOUT_S,
    retries={"mode": "standard", "total_max_attempts": 1},
)


def _response(text: str, usage: dict, blocks: dict | None = None) -> dict:
    out = {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": usage,
    }
    if blocks and len(blocks.get("Blocks", [])) > 1:
        out["textractBlocks"] = blocks
    return out


def _default_detectors(textract_client) -> dict[str, Callable]:
    return {
        "paddle": lambda img: paddle_det.detect(img),
        "textract": lambda img, w, h: textract_det.detect(textract_client, img, w, h),
    }


def _run_detectors(
    dets: dict[str, Callable], req: HookRequest, deadline: float
) -> list[Box]:
    """Call each detector in isolation: a crash degrades that source only."""
    boxes: list[Box] = []
    for name in _DETECTOR_ORDER:
        fn = dets.get(name)
        if fn is None:
            continue
        if deadline - time.monotonic() < DETECTOR_BUDGET_S.get(name, 60.0):
            metrics.emit("DetectorDegraded", reason="DeadlineSkip", detector=name)
            logger.warning("detector skipped, deadline too close: %s", name)
            continue
        try:
            found = (
                fn(req.image_bytes, req.width, req.height)
                if name == "textract"
                else fn(req.image_bytes)
            )
            boxes.extend(found)
        except TextractUnavailable as exc:
            # str(exc) here is the throttle/error code TextractUnavailable was
            # constructed with (e.g. "ThrottlingException"), never document text.
            metrics.emit("TextractDegraded", reason=str(exc)[:64])
            logger.warning("detector degraded (no document text logged): %s", name)
        except Exception as exc:
            metrics.emit(
                "DetectorDegraded", reason=type(exc).__name__[:40], detector=name
            )
            logger.warning("detector degraded (no document text logged): %s", name)
    return boxes


def _m0_lines(regions: list[Region], transcription: str = "") -> list[tuple[Box, str]]:
    """M0: each region's box AND text come straight from the detectors that
    found it (no reader call).

    On an Arabic region, PaddleOCR's Arabic recognizer reads only the Arabic,
    so the Latin and numeric words Textract read in the same region (for
    example "Serial No. UTA 7712 0045" beside an Arabic label) are kept too -
    but only words that also occur verbatim in the transcription, so a Textract
    misreading never reaches the line text. PaddleOCR Arabic text also gets its
    dropped digits back from the transcription (snap.snap_to_transcription),
    which never changes the reading otherwise.
    """
    known_words = set(transcription.split())
    lines: list[tuple[Box, str]] = []

    def add(box: Box, text: str) -> None:
        # same exact-duplicate-on-one-row rule as the M1/M2/M3 path below.
        if any(
            text == et and vertical_overlap_ratio(box, eb) >= 0.5 for eb, et in lines
        ):
            return
        lines.append((box, text))

    for region in regions:
        pm = sorted(
            (m for m in region.members if m.source == "paddle"), key=lambda b: -b.x1
        )
        tm = sorted(
            (m for m in region.members if m.source == "textract"), key=lambda b: b.x0
        )
        ptext = " ".join(m.text for m in pm if m.text).strip()
        ttext = " ".join(m.text for m in tm if m.text).strip()
        if "paddle" in region.boxes and ptext and classify(ptext) == "ar":
            arabic = snap_to_transcription(ptext, transcription)
            latin = [
                w
                for w in ttext.split()
                if _LATIN_OR_DIGIT.search(w) and w in known_words and w not in arabic
            ]
            add(region.boxes["paddle"], " ".join([*latin, arabic]))
        elif "textract" in region.boxes and ttext:
            add(region.boxes["textract"], ttext)
        elif "paddle" in region.boxes and ptext:
            add(region.boxes["paddle"], ptext)
    return lines


def process_page(
    req: HookRequest,
    settings: Settings,
    bedrock,
    textract_client,
    deadline: float,
    detectors: dict[str, Callable] | None = None,
    reader_client=None,
) -> dict:
    # Full-page transcription: exceptions here PROPAGATE so the IDP retries
    # throttles exactly as it does for its own Bedrock OCR.
    text, usage = transcribe_page(bedrock, settings, req)
    try:
        dets = (
            detectors if detectors is not None else _default_detectors(textract_client)
        )
        boxes = _run_detectors(dets, req, deadline)
        regions = build_regions(boxes)
        if not regions:
            return _response(text, usage.as_dict())
        if settings.read_method == "M0":
            # M0: boxes AND text both come from the detectors; never call
            # the reader.
            lines = _m0_lines(regions, text)
            blocks = to_textract_blocks(lines, req.width, req.height)
            metrics.emit("BoxLines", len(blocks["Blocks"]) - 1)
            return _response(text, usage.as_dict(), blocks)
        if time.monotonic() > deadline:
            return _response(text, usage.as_dict())
        # the reader gets its own (tighter) client and time budget, so a
        # slow reader call cannot silently consume the whole remaining
        # deadline; read_regions raises ReaderDeadlineExceeded (caught below)
        # rather than block past it.
        client = reader_client if reader_client is not None else bedrock
        texts, read_usage = read_regions(
            client,
            settings,
            req,
            regions,
            deadline=deadline,
            min_call_budget_s=READER_READ_TIMEOUT_S + 10,
        )
        usage = usage + read_usage
        lines: list[tuple[Box, str]] = []
        for region in regions:
            t = texts.get(region.id, "").strip()
            if not t:
                continue
            box = choose_box(region, classify(t))
            if box is None:
                continue
            # drop an exact-duplicate line (same text, same row) rather than
            # emit two overlapping LINE blocks for one physical line.
            if any(
                t == emitted_text and vertical_overlap_ratio(box, emitted_box) >= 0.5
                for emitted_box, emitted_text in lines
            ):
                continue
            lines.append((box, t))
        blocks = to_textract_blocks(lines, req.width, req.height)
        metrics.emit("BoxLines", len(blocks["Blocks"]) - 1)
        return _response(text, usage.as_dict(), blocks)
    except Exception as exc:  # never worse than today: text without boxes
        logger.warning(
            "boxes degraded (no document text logged): %s", type(exc).__name__
        )
        metrics.emit("BoxesDegraded", reason=type(exc).__name__[:40])
        # note: if read_regions raised (e.g. ReaderDeadlineExceeded), `usage`
        # still holds only the transcription call's usage; any reader-call tokens spent
        # before the failure are an accepted accounting gap, not reported.
        return _response(text, usage.as_dict())


def lambda_handler(event, context=None):
    metrics.emit("Pages")
    settings = Settings.from_env()
    if "bedrock-runtime" not in _CLIENTS:
        _CLIENTS["bedrock-runtime"] = boto3.client(
            "bedrock-runtime", config=_TRANSCRIBE_CONFIG
        )
    if settings.read_method != "M0" and "bedrock-reader" not in _CLIENTS:
        _CLIENTS["bedrock-reader"] = boto3.client(
            "bedrock-runtime", config=_READER_CONFIG
        )
    if "s3" not in _CLIENTS:
        _CLIENTS["s3"] = boto3.client("s3")
    if "textract" not in _CLIENTS:
        _CLIENTS["textract"] = textract_det.make_client(settings.textract_max_attempts)
    req = parse_request(event, _CLIENTS["s3"])
    remaining_s = context.get_remaining_time_in_millis() / 1000.0 if context else 840.0
    deadline = time.monotonic() + max(0.0, remaining_s - settings.safety_margin_s)
    return process_page(
        req,
        settings,
        _CLIENTS["bedrock-runtime"],
        _CLIENTS["textract"],
        deadline,
        reader_client=_CLIENTS.get("bedrock-reader"),
    )
