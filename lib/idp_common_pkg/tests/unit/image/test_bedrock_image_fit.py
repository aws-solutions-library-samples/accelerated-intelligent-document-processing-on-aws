# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock enforces its 5 MiB per-image limit on the BASE64 payload (#778).

The bug this suite guards: comparing RAW bytes to the limit. A 4 MB PNG is
5.3 MB once encoded and Bedrock rejects it with a hard ValidationException on
the whole request — measured on a shipped corpus, 11 of 293 documents failed
that way, four of them with source images comfortably under 5 MB. The fit must
therefore reason in encoded bytes, downscale until it fits, preserve the format
where it can, and hand back a record a caller can persist.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from idp_common import image as image_mod
from idp_common.image import (
    BEDROCK_IMAGE_MAX_DIMENSION,
    BEDROCK_IMAGE_MAX_ENCODED_BYTES,
    BEDROCK_IMAGE_MAX_RAW_BYTES,
    ImageFit,
    base64_encoded_size,
    fit_image_to_bedrock_limit,
    prepare_bedrock_image_attachment,
)

pytestmark = pytest.mark.unit


def _noise_png(width: int, height: int, seed: int = 7) -> bytes:
    """A PNG that will NOT compress: random RGB noise. Bytes ≈ 3 × pixels."""
    import random

    rng = random.Random(seed)
    img = Image.frombytes(
        "RGB",
        (width, height),
        bytes(rng.getrandbits(8) for _ in range(width * height * 3)),
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _flat_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------------------------------------------- arithmetic


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 6, 100, 3_932_160, 3_932_161])
def test_base64_encoded_size_matches_real_encoding(n: int) -> None:
    assert base64_encoded_size(n) == len(base64.b64encode(b"x" * n))


def test_raw_budget_is_three_quarters_of_the_encoded_limit() -> None:
    """3.75 MiB, not 5 MiB — the number the docs must quote."""
    assert BEDROCK_IMAGE_MAX_ENCODED_BYTES == 5 * 1024 * 1024
    assert BEDROCK_IMAGE_MAX_RAW_BYTES == 3_932_160
    assert (
        base64_encoded_size(BEDROCK_IMAGE_MAX_RAW_BYTES)
        == BEDROCK_IMAGE_MAX_ENCODED_BYTES
    )
    assert (
        base64_encoded_size(BEDROCK_IMAGE_MAX_RAW_BYTES + 1)
        > BEDROCK_IMAGE_MAX_ENCODED_BYTES
    )


def test_corpus_boundary_from_the_issue() -> None:
    """The largest page that succeeded and the smallest that failed on the
    ocr-benchmark corpus straddle the raw budget; the fit must agree."""
    assert (
        base64_encoded_size(3_839_952) <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    )  # succeeded
    assert base64_encoded_size(4_003_471) > BEDROCK_IMAGE_MAX_ENCODED_BYTES  # failed
    # And Bedrock's reported byte count is exactly the encoded size.
    assert base64_encoded_size(5_375_889) == 7_167_852


# ------------------------------------------------------------------- fitting


def test_image_within_budget_is_returned_byte_identical() -> None:
    data = _flat_png(800, 1000)
    out, fit = fit_image_to_bedrock_limit(data)
    assert out is data
    assert fit is None


def test_raw_size_under_limit_but_encoded_over_is_downscaled() -> None:
    """THE bug: an image under 5 MiB raw whose encoded payload exceeds 5 MiB."""
    # ~1180x1180 RGB noise ≈ 4.2 MB raw PNG — under 5 MiB, over 3.75 MiB.
    data = _noise_png(1180, 1180)
    raw = len(data)
    assert BEDROCK_IMAGE_MAX_RAW_BYTES < raw < BEDROCK_IMAGE_MAX_ENCODED_BYTES, raw
    assert base64_encoded_size(raw) > BEDROCK_IMAGE_MAX_ENCODED_BYTES

    out, fit = fit_image_to_bedrock_limit(data)

    assert fit is not None
    assert base64_encoded_size(len(out)) <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    assert fit.final_encoded_bytes == base64_encoded_size(len(out))
    assert fit.original_bytes == raw
    assert fit.original_encoded_bytes == base64_encoded_size(raw)
    assert (fit.final_width, fit.final_height) < (1180, 1180)
    assert "encoded size" in fit.reason
    # Aspect ratio preserved (square in, square out).
    assert abs(fit.final_width - fit.final_height) <= 1


def test_png_is_kept_as_png_when_a_proportional_shrink_suffices() -> None:
    """A page only a little over budget shrinks a few percent and stays PNG;
    JPEG is a last resort, not the first move, because text pages are PNG."""
    data = _noise_png(1180, 1180)
    out, fit = fit_image_to_bedrock_limit(data)
    assert fit is not None
    assert fit.final_format == "PNG"
    assert Image.open(io.BytesIO(out)).format == "PNG"
    assert fit.passes == 1


def test_falls_back_to_jpeg_after_the_native_format_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PNG compression is non-linear, so a proportional shrink can land over
    budget; after the lossless passes the fit must switch to JPEG rather than
    keep shaving PNG. Deterministic by giving PNG zero native passes."""
    monkeypatch.setattr(image_mod, "_FIT_NATIVE_FORMAT_PASSES", 0)
    data = _noise_png(600, 600)
    tiny = 40_000
    out, fit = fit_image_to_bedrock_limit(data, max_encoded_bytes=tiny)
    assert fit is not None
    assert base64_encoded_size(len(out)) <= tiny
    assert fit.final_format == "JPEG"
    assert fit.original_format == "PNG"
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_a_tiny_budget_still_fits_in_one_proportional_png_pass() -> None:
    """The size-ratio estimate (sqrt of the byte ratio) lands a 600 px noise PNG
    under a 40 KB budget in ONE pass, still as PNG — the estimate, not the
    per-pass floor, does the work."""
    data = _noise_png(600, 600)
    out, fit = fit_image_to_bedrock_limit(data, max_encoded_bytes=40_000)
    assert fit is not None
    assert fit.passes == 1 and fit.final_format == "PNG"
    assert base64_encoded_size(len(out)) <= 40_000


def test_oversize_dimension_is_downscaled_even_when_bytes_fit() -> None:
    """Claude rejects >8000 px on a side regardless of size; a flat 9000 px PNG
    is tiny in bytes and would otherwise sail through."""
    data = _flat_png(9000, 100)
    assert base64_encoded_size(len(data)) <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    out, fit = fit_image_to_bedrock_limit(data)
    assert fit is not None
    assert fit.final_width <= BEDROCK_IMAGE_MAX_DIMENSION
    assert "px per side" in fit.reason
    assert Image.open(io.BytesIO(out)).size[0] <= BEDROCK_IMAGE_MAX_DIMENSION


def test_rgba_png_forced_to_jpeg_is_flattened_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    img = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
    import random

    px = img.load()
    rng = random.Random(3)
    for x in range(600):
        for y in range(600):
            px[x, y] = (rng.getrandbits(8), rng.getrandbits(8), rng.getrandbits(8), 128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    monkeypatch.setattr(image_mod, "_FIT_NATIVE_FORMAT_PASSES", 0)
    out, fit = fit_image_to_bedrock_limit(buf.getvalue(), max_encoded_bytes=40_000)
    assert fit is not None and fit.final_format == "JPEG"
    assert Image.open(io.BytesIO(out)).mode == "RGB"


def test_unreadable_bytes_pass_through_for_the_format_check_downstream() -> None:
    """Fitting is best-effort; the existing 'Unsupported image format' error in
    prepare_bedrock_image_attachment stays the place that rejects garbage."""
    out, fit = fit_image_to_bedrock_limit(b"not an image")
    assert out == b"not an image" and fit is None
    with pytest.raises(Exception):
        prepare_bedrock_image_attachment(b"not an image")


def test_gives_up_loudly_when_it_cannot_fit() -> None:
    data = _noise_png(400, 400)
    with pytest.raises(ValueError, match="could not be reduced"):
        fit_image_to_bedrock_limit(data, max_encoded_bytes=16)


def test_fit_record_round_trips_to_a_plain_dict() -> None:
    data = _noise_png(1180, 1180)
    _, fit = fit_image_to_bedrock_limit(data)
    assert isinstance(fit, ImageFit)
    d = fit.to_dict()
    assert set(d) >= {
        "original_bytes",
        "original_encoded_bytes",
        "final_bytes",
        "final_encoded_bytes",
        "final_width",
        "final_height",
        "final_format",
        "passes",
        "reason",
    }


# --------------------------------------------------------- the choke point


def test_prepare_bedrock_image_attachment_never_emits_an_oversize_image() -> None:
    """Every Bedrock image in the pipeline passes through this function, so it
    is the one place that guarantees the payload Bedrock sees fits."""
    data = _noise_png(1180, 1180)
    block = prepare_bedrock_image_attachment(data)
    sent = block["image"]["source"]["bytes"]
    assert base64_encoded_size(len(sent)) <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    assert block["image"]["format"] == "png"


def test_prepare_bedrock_image_attachment_is_a_pass_through_when_it_fits() -> None:
    data = _flat_png(800, 1000)
    block = prepare_bedrock_image_attachment(data)
    assert block["image"]["source"]["bytes"] is data


# ------------------------------------------------- modes Pillow would mangle


def _bilevel_png(width: int, height: int) -> bytes:
    """A mode-"1" (bilevel) noise PNG, like a fax/TIFF-derived scan."""
    import random

    rng = random.Random(11)
    img = Image.new("1", (width, height))
    img.putdata([rng.getrandbits(1) for _ in range(width * height)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_bilevel_image_is_converted_so_lanczos_is_used_not_nearest() -> None:
    """Pillow silently swaps LANCZOS for NEAREST on mode "1"/"P"; pixel-dropping
    a scanned text page breaks thin strokes. The fit must convert first — the
    observable evidence is that the output is grayscale, not bilevel."""
    data = _bilevel_png(9000, 200)  # over the 8,000 px cap, tiny in bytes
    out, fit = fit_image_to_bedrock_limit(data)
    assert fit is not None
    result = Image.open(io.BytesIO(out))
    assert result.mode == "L", result.mode
    assert result.size[0] <= BEDROCK_IMAGE_MAX_DIMENSION


def test_palette_image_is_converted_before_resizing() -> None:
    img = Image.new("RGB", (9000, 100), (200, 30, 30)).convert("P")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out, fit = fit_image_to_bedrock_limit(buf.getvalue())
    assert fit is not None
    assert Image.open(io.BytesIO(out)).mode in ("RGB", "RGBA")


def test_multi_pass_resizes_from_the_original_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each pass resamples the ORIGINAL at the running product of scales. With
    the size estimate neutralised, every pass is exactly the step floor, so the
    final width must be original × floor**passes computed once — not the drift
    of repeatedly truncating an already-resized image."""
    monkeypatch.setattr(image_mod, "_FIT_TARGET_FRACTION", 1e9)  # size_ratio huge
    monkeypatch.setattr(image_mod, "_FIT_MAX_STEP_RATIO", 0.5)  # so scale == 0.5
    data = _noise_png(600, 600)
    out, fit = fit_image_to_bedrock_limit(data, max_encoded_bytes=200_000)
    assert fit is not None and fit.passes >= 2, fit
    assert fit.final_width == int(600 * 0.5**fit.passes)
    assert Image.open(io.BytesIO(out)).size[0] == fit.final_width
