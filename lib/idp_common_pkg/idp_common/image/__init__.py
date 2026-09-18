# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import io
import logging
import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterator, Optional, Tuple, Union

from PIL import Image, ImageFilter, UnidentifiedImageError

from ..s3 import get_binary_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bedrock per-image budget (#778)
# ---------------------------------------------------------------------------
# The Converse API rejects an image whose payload exceeds 5 MiB — and it
# measures the BASE64-ENCODED payload, not the raw bytes, so the effective raw
# budget is 5 MiB / (4/3) = 3.75 MiB. Measured on a shipped corpus: every stored
# page image above 3,932,160 bytes failed ("image exceeds 5 MB maximum:
# 7167852 bytes > 5242880 bytes", reported bytes = raw × 4/3 exactly) and every
# one below it succeeded, with zero documents on the wrong side. The rejection
# is a hard ValidationException on the whole request — no downscale, no retry —
# so the check has to happen here, where the image is prepared.
BEDROCK_IMAGE_MAX_ENCODED_BYTES = 5 * 1024 * 1024  # 5,242,880
BEDROCK_IMAGE_MAX_RAW_BYTES = BEDROCK_IMAGE_MAX_ENCODED_BYTES // 4 * 3  # 3,932,160
# The Converse API rejects any image over 8,000 px on a side regardless of byte
# size (documented on the Message reference: "no more than 3.75 MB, 8000 px, and
# 8000 px"); it applies to every model routed through Converse, not only Claude.
BEDROCK_IMAGE_MAX_DIMENSION = 8000
# A SECOND, stricter dimension cap applies to a request that carries MANY images
# (#994). Anthropic's vision reference: "If a single API request contains more
# than 20 images, a stricter per-image dimension limit applies to every image in
# that request … resize each image so that neither dimension exceeds 2000 px, or
# keep the request to 20 or fewer image and document blocks." All image blocks
# count, including images returned inside tool results and — on Bedrock —
# document blocks. Measured on 29 stored pages, us.anthropic.claude-sonnet-5,
# us-west-2: 2,001 px REJECTED ("exceed max allowed size for many-image
# requests: 2000 pixels"), 2,000 px ACCEPTED (117,035 input tokens); the same
# pages at 2,150 px were ACCEPTED in a 5-image request. So the cap binds on the
# request's image COUNT, which a per-image guard structurally cannot see — every
# page is individually fine. fit_images_in_request applies it where the count is
# known. The values are Claude's, and they are the FLOOR across families (Nova
# documents a 25 MB total-payload budget and no stricter per-image cap), so
# applying them uniformly cannot cause a rejection that would not otherwise
# happen; it costs ~15% of the image tokens, because Converse downscales to
# roughly this size before tokenizing anyway.
BEDROCK_MANY_IMAGE_MAX_DIMENSION = 2000
BEDROCK_MANY_IMAGE_COUNT_THRESHOLD = 20
# Aim a little under the limit so PNG's non-linear compression cannot land a
# resized image a few hundred bytes over and force another pass.
_FIT_TARGET_FRACTION = 0.92
# Never shrink by less than this per pass, so the loop always makes progress.
_FIT_MAX_STEP_RATIO = 0.95
# Bound the loop: a pathological image that will not compress must raise a
# clear error rather than spin. Nine passes at ≤0.95 is ~0.63 linear scale
# (~0.4 pixel area) AFTER the size-ratio estimate, far more than any real page
# has needed.
_FIT_MAX_PASSES = 9
# Lossless formats get this many passes at their native format before the fit
# falls back to JPEG; a text page that is still over after two proportional
# PNG shrinks is photographic or noisy enough that JPEG is the right tool.
_FIT_NATIVE_FORMAT_PASSES = 2
_FIT_JPEG_QUALITY = 90


def base64_encoded_size(raw_size: int) -> int:
    """Bytes a payload of ``raw_size`` occupies once base64-encoded (with padding).

    Bedrock enforces its per-image limit against this number, not the raw
    length — comparing raw bytes to the limit is the bug this module guards
    against.
    """
    return (raw_size + 2) // 3 * 4


def max_dimension_for_image_count(image_count: int) -> int:
    """The per-image dimension cap that applies to a request of ``image_count`` images.

    ``BEDROCK_IMAGE_MAX_DIMENSION`` (8,000 px) normally, dropping to
    ``BEDROCK_MANY_IMAGE_MAX_DIMENSION`` (2,000 px) once the request carries
    more than ``BEDROCK_MANY_IMAGE_COUNT_THRESHOLD`` image blocks. Count every
    image the request will carry, not just page images.
    """
    return (
        BEDROCK_MANY_IMAGE_MAX_DIMENSION
        if image_count > BEDROCK_MANY_IMAGE_COUNT_THRESHOLD
        else BEDROCK_IMAGE_MAX_DIMENSION
    )


@dataclass(frozen=True)
class ImageFit:
    """Record of an image having been shrunk to fit Bedrock's per-image limit.

    Returned alongside the fitted bytes so a caller can persist it (extraction
    writes it into section metadata as ``image_downscale``) — a page that was
    silently sent at a lower resolution than stored must be auditable, the way
    coercion and forced-tool decisions already are.
    """

    original_bytes: int
    original_encoded_bytes: int
    original_width: int
    original_height: int
    original_format: str
    final_bytes: int
    final_encoded_bytes: int
    final_width: int
    final_height: int
    final_format: str
    passes: int
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fit_reason(
    encoded: int, width: int, height: int, max_encoded: int, max_dimension: int
) -> Optional[str]:
    reasons = []
    if encoded > max_encoded:
        reasons.append(
            f"encoded size {encoded:,} B > {max_encoded:,} B limit "
            f"(raw budget {max_encoded // 4 * 3:,} B)"
        )
    if max(width, height) > max_dimension:
        reasons.append(f"{width}x{height} px exceeds {max_dimension} px per side")
    return "; ".join(reasons) or None


def fit_image_to_bedrock_limit(
    image_data: bytes,
    max_encoded_bytes: int = BEDROCK_IMAGE_MAX_ENCODED_BYTES,
    max_dimension: int = BEDROCK_IMAGE_MAX_DIMENSION,
    log_fit: bool = True,
) -> Tuple[bytes, Optional[ImageFit]]:
    """Shrink ``image_data`` until it fits Bedrock's per-image limits.

    The limit is compared against the BASE64-ENCODED size (see
    ``base64_encoded_size``). An image already within budget is returned
    unchanged with ``None``; otherwise it is downscaled proportionally
    (LANCZOS) in as few passes as possible — first in its own format, then, for
    lossless formats that are still over, re-encoded as JPEG — and the fitted
    bytes are returned with an :class:`ImageFit` describing what changed. A
    slightly smaller page is strictly better than a document that fails.

    Raises ``ValueError`` if the image still does not fit after the bounded
    number of passes, naming the sizes, so the failure is attributable instead
    of surfacing as Bedrock's generic ValidationException.

    ``log_fit=False`` suppresses the per-image warning for callers that fit many
    images at once and log one aggregate line instead
    (``fit_images_in_request``); 25 identical warnings say nothing the summary
    does not.
    """
    encoded = base64_encoded_size(len(image_data))
    try:
        img = Image.open(io.BytesIO(image_data))
    except UnidentifiedImageError:
        # Not something PIL can read. Fitting is best-effort; leave format
        # validation to prepare_bedrock_image_attachment, which raises the
        # existing "Unsupported image format" error for it.
        return image_data, None
    width, height = img.size
    original_format = img.format or "UNKNOWN"
    reason = _fit_reason(encoded, width, height, max_encoded_bytes, max_dimension)
    if reason is None:
        return image_data, None

    original = (len(image_data), encoded, width, height, original_format)
    # Pillow silently falls back to NEAREST for mode "1" (bilevel) and "P"
    # (palette) images, which would downscale a scanned text page by dropping
    # pixels and break thin strokes. Convert first so LANCZOS really is used.
    source = img
    if img.mode == "1":
        source = img.convert("L")
    elif img.mode == "P":
        source = img.convert("RGBA" if "transparency" in img.info else "RGB")
    src_w, src_h = source.size
    data = image_data
    fmt = original_format
    cumulative_scale = 1.0
    for passes in range(1, _FIT_MAX_PASSES + 1):
        # Bytes scale roughly with pixel area, so the linear factor is the
        # square root of the size ratio; the dimension cap is linear. Always
        # shrink by at least a little so the loop cannot stall.
        size_ratio = math.sqrt(max_encoded_bytes * _FIT_TARGET_FRACTION / encoded)
        dim_ratio = max_dimension / max(width, height)
        if dim_ratio < 1.0 and dim_ratio <= size_ratio:
            # The dimension cap binds, and unlike the byte estimate it is exact:
            # resizing to precisely the cap is guaranteed to satisfy it, so the
            # _FIT_MAX_STEP_RATIO floor must not be allowed to overshoot it. It
            # would: a page only 48px over the cap (2048 -> 2000, ratio 0.977)
            # would be taken to 1945px instead, discarding ~2.7% more resolution
            # than required. That became the common case once the 2,000px
            # many-image cap started binding on ordinary pages (#994).
            scale = dim_ratio
        else:
            scale = min(size_ratio, _FIT_MAX_STEP_RATIO)
        # Resize from the ORIGINAL pixels at the running product of scales, so
        # a multi-pass fit does not compound resampling loss pass over pass.
        cumulative_scale *= scale
        new_w = max(1, int(src_w * cumulative_scale))
        new_h = max(1, int(src_h * cumulative_scale))

        use_jpeg = fmt == "JPEG" or passes > _FIT_NATIVE_FORMAT_PASSES
        save_fmt = "JPEG" if use_jpeg else fmt
        if save_fmt not in ("JPEG", "PNG", "GIF", "WEBP"):
            save_fmt = "JPEG"  # Bedrock accepts only these four anyway

        resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)
        if save_fmt == "JPEG" and resized.mode not in ("RGB", "L"):
            # JPEG has no alpha / palette; flatten onto white like a printed page.
            if "A" in resized.mode or resized.mode == "P":
                rgba = resized.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                resized = background
            else:
                resized = resized.convert("RGB")
        elif save_fmt == "PNG" and resized.mode == "CMYK":
            resized = resized.convert("RGB")

        buf = io.BytesIO()
        save_kwargs: Dict[str, Any] = {"format": save_fmt}
        if save_fmt == "JPEG":
            save_kwargs.update(quality=_FIT_JPEG_QUALITY, optimize=True)
        # PNG deliberately uses Pillow's default compression: optimize=True
        # measured 2.5x slower per pass on a real page with no size gain.
        resized.save(buf, **save_kwargs)

        data = buf.getvalue()
        encoded = base64_encoded_size(len(data))
        width, height, fmt = new_w, new_h, save_fmt
        if (
            _fit_reason(encoded, width, height, max_encoded_bytes, max_dimension)
            is None
        ):
            fit = ImageFit(
                original_bytes=original[0],
                original_encoded_bytes=original[1],
                original_width=original[2],
                original_height=original[3],
                original_format=original[4],
                final_bytes=len(data),
                final_encoded_bytes=encoded,
                final_width=width,
                final_height=height,
                final_format=fmt,
                passes=passes,
                reason=reason,
            )
            if not log_fit:
                return data, fit
            logger.warning(
                "Page image exceeded Bedrock's per-image limit (%s); downscaled "
                "%dx%d %s (%s B) -> %dx%d %s (%s B, %s B encoded) in %d pass(es) "
                "so the request can succeed. Set the stage's "
                "image.target_width/target_height so pages render inside the "
                "budget and avoid this per request.",
                reason,
                original[2],
                original[3],
                original[4],
                f"{original[0]:,}",
                width,
                height,
                fmt,
                f"{len(data):,}",
                f"{encoded:,}",
                passes,
            )
            return data, fit

    raise ValueError(
        f"Image could not be reduced to fit Bedrock's per-image limit after "
        f"{_FIT_MAX_PASSES} passes: started {original[2]}x{original[3]} "
        f"{original[4]} {original[0]:,} B ({original[1]:,} B encoded), ended "
        f"{width}x{height} {fmt} {len(data):,} B ({encoded:,} B encoded) against "
        f"{max_encoded_bytes:,} B encoded / {max_dimension} px limits."
    )


def _walk_content_blocks(content: Any) -> Iterator[Dict[str, Any]]:
    """Yield every Converse content block in ``content``, tool results included.

    Images nested in a ``toolResult`` count toward the many-image threshold the
    same as top-level ones (the agentic ``view_image`` tool returns pages this
    way), so the walk has to descend into them.
    """
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        yield block
        tool_result = block.get("toolResult")
        if isinstance(tool_result, dict):
            yield from _walk_content_blocks(tool_result.get("content"))


def count_request_image_blocks(messages: Any) -> int:
    """Count the blocks in one Converse request that tell against the cap.

    Both ``image`` and ``document`` blocks count on Bedrock, at every depth a
    ``toolResult`` can nest them. Split out from
    :func:`fit_images_in_request` so a caller can decide whether the cap binds
    — and therefore whether it needs to copy anything — before paying for a
    copy of the request (see ``BedrockClient._fit_request_images``).
    """
    return sum(
        1
        for message in (messages or [])
        if isinstance(message, dict)
        for block in _walk_content_blocks(message.get("content"))
        if isinstance(block.get("image"), dict) or isinstance(block.get("document"), dict)
    )


def fit_images_in_request(messages: Any) -> int:
    """Apply the many-image dimension cap across one Converse request, in place.

    Counts the image (and, per the Bedrock note in
    ``BEDROCK_MANY_IMAGE_MAX_DIMENSION``, document) blocks the request carries
    and, when that count is over the threshold, downscales any image above
    2,000 px per side. This is the only place that can enforce the cap
    correctly: it binds on the request's image COUNT, so a per-image guard —
    ``fit_image_to_bedrock_limit`` at ``prepare_bedrock_image_attachment`` —
    passes every individually-legal page and the request fails as a whole.

    Best-effort by design: a page that cannot be fitted is left alone with a
    warning rather than raising, because the alternative is failing a request
    that Bedrock might still have accepted.

    ⚠️ **Mutates in place.** The image blocks and their ``source`` dicts are
    rewritten where they sit, so a caller that owns or reuses those dicts — a
    cached few-shot example, a page-image list reused by a later pass — sees the
    downscaled bytes afterwards. Copy the request spine first if that matters;
    ``BedrockClient._fit_request_images`` does, because ``invoke_model`` is
    handed the caller's own ``content`` list.

    Args:
        messages: The Converse ``messages`` list (mutated in place).

    Returns:
        Number of images downscaled.
    """
    image_blocks = [
        block
        for message in (messages or [])
        if isinstance(message, dict)
        for block in _walk_content_blocks(message.get("content"))
        if isinstance(block.get("image"), dict)
    ]
    counted = count_request_image_blocks(messages)
    max_dimension = max_dimension_for_image_count(counted)
    if max_dimension >= BEDROCK_IMAGE_MAX_DIMENSION:
        return 0

    clamped = 0
    for block in image_blocks:
        image_block = block["image"]
        source = image_block.get("source")
        if not isinstance(source, dict):
            continue  # s3Location source: Bedrock fetches it, we cannot resize it
        data = source.get("bytes")
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        try:
            fitted, fit = fit_image_to_bedrock_limit(
                bytes(data), max_dimension=max_dimension, log_fit=False
            )
            if fit is None:
                continue
            # Resolve the new format BEFORE swapping the bytes in: a lossless
            # page can come back as JPEG, and a block whose declared format does
            # not match its bytes is rejected outright. If this raises, the
            # ``except`` below must be able to honestly say the image was left
            # unchanged — which it cannot if the bytes are already replaced.
            fitted_format = bedrock_image_format(fitted)
            source["bytes"] = fitted
            image_block["format"] = fitted_format
        except Exception as e:  # noqa: BLE001 - never fail a request over the guard
            logger.warning(
                "Could not downscale an image to the %d px many-image cap (%s); "
                "sending it unchanged. Bedrock may reject the request.",
                max_dimension,
                e,
            )
            continue
        clamped += 1

    if clamped:
        logger.warning(
            "Request carries %d image/document block(s), over Bedrock's "
            "many-image threshold of %d, so the stricter %d px per-side cap "
            "applies to EVERY image in it; downscaled %d image(s) to fit. Set "
            "the stage's image.target_width/target_height to %d (or fewer pages "
            "per request) to avoid the re-encode.",
            counted,
            BEDROCK_MANY_IMAGE_COUNT_THRESHOLD,
            max_dimension,
            clamped,
            BEDROCK_MANY_IMAGE_MAX_DIMENSION,
        )
    return clamped


def resize_image(
    image_data: bytes,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    allow_upscale: bool = False,
) -> bytes:
    """
    Resize an image to fit within target dimensions while preserving aspect ratio.
    No padding, no distortion - pure proportional scaling.
    Preserves original format when possible.

    Args:
        image_data: Raw image bytes
        target_width: Target width in pixels (None or empty string = no resize)
        target_height: Target height in pixels (None or empty string = no resize)
        allow_upscale: Whether to allow making the image larger than original

    Returns:
        Resized image bytes in original format (or JPEG if format cannot be preserved)
    """
    # Handle empty strings - convert to None
    if isinstance(target_width, str) and not target_width.strip():
        target_width = None
    if isinstance(target_height, str) and not target_height.strip():
        target_height = None

    # If BOTH dimensions are None, return original image unchanged
    if target_width is None and target_height is None:
        logger.info(
            "No resize requested (both dimensions are None), returning original image"
        )
        return image_data

    # Convert to int if needed (before opening image)
    try:
        if target_width is not None:
            target_width = int(target_width)
        if target_height is not None:
            target_height = int(target_height)
    except (ValueError, TypeError):
        logger.warning(
            f"Invalid resize dimensions: width={target_width}, height={target_height}, returning original image"
        )
        return image_data

    # Open image to get dimensions and calculate missing dimension if needed
    image = Image.open(io.BytesIO(image_data))
    current_width, current_height = image.size
    original_format = image.format  # Store original format

    # Calculate missing dimension if only one provided (preserving aspect ratio)
    if target_width is None and target_height is not None:
        # Only height provided - calculate width preserving aspect ratio
        aspect_ratio = current_width / current_height
        target_width = int(target_height * aspect_ratio)
        logger.info(
            f"Calculated target_width={target_width} from target_height={target_height} (aspect={aspect_ratio:.3f})"
        )
    elif target_height is None and target_width is not None:
        # Only width provided - calculate height preserving aspect ratio
        aspect_ratio = current_height / current_width
        target_height = int(target_width * aspect_ratio)
        logger.info(
            f"Calculated target_height={target_height} from target_width={target_width} (aspect={aspect_ratio:.3f})"
        )

    # At this point, both dimensions must be set (type guard for Pylance)
    assert target_width is not None and target_height is not None, (
        "Both dimensions should be set after calculation"
    )

    # Calculate scaling factor to fit within bounds while preserving aspect ratio
    width_ratio = target_width / current_width
    height_ratio = target_height / current_height
    scale_factor = min(width_ratio, height_ratio)  # Fit within bounds

    # Determine if resizing is needed
    needs_resize = (scale_factor < 1.0) or (allow_upscale and scale_factor > 1.0)

    if needs_resize:
        new_width = int(current_width * scale_factor)
        new_height = int(current_height * scale_factor)
        logger.info(
            f"Resizing image from {current_width}x{current_height} to {new_width}x{new_height} (scale: {scale_factor:.3f})"
        )
        image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Save in original format if possible
        img_byte_array = io.BytesIO()

        # Determine save format - use original if available, otherwise JPEG
        if original_format and original_format in [
            "JPEG",
            "PNG",
            "GIF",
            "BMP",
            "TIFF",
            "WEBP",
        ]:
            save_format = original_format
        else:
            save_format = "JPEG"
            logger.info(f"Converting from {original_format or 'unknown'} to JPEG")

        # Prepare save parameters
        save_kwargs = {"format": save_format}

        # Add quality parameters for JPEG
        if save_format in ["JPEG", "JPG"]:
            save_kwargs["quality"] = 95  # High quality
            save_kwargs["optimize"] = True

        # Handle format-specific requirements
        if save_format == "PNG" and image.mode not in ["RGBA", "LA", "L", "P"]:
            # PNG requires specific modes
            if image.mode == "CMYK":
                image = image.convert("RGB")

        image.save(img_byte_array, **save_kwargs)
        return img_byte_array.getvalue()
    else:
        # No resizing needed - return original data unchanged
        logger.info(
            f"Image {current_width}x{current_height} already fits within {target_width}x{target_height}, returning original"
        )
        return image_data


def prepare_image(
    image_source: Union[str, bytes],
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    allow_upscale: bool = False,
) -> bytes:
    """
    Prepare an image for model input from either S3 URI or raw bytes

    Args:
        image_source: Either an S3 URI (s3://bucket/key) or raw image bytes
        target_width: Target width in pixels (None or empty string = no resize)
        target_height: Target height in pixels (None or empty string = no resize)
        allow_upscale: Whether to allow making the image larger than original

    Returns:
        Processed image bytes ready for model input (preserves format when possible)
    """
    # Get the image data
    if isinstance(image_source, str) and image_source.startswith("s3://"):
        image_data = get_binary_content(image_source)
    elif isinstance(image_source, bytes):
        image_data = image_source
    else:
        raise ValueError(
            f"Invalid image source: {type(image_source)}. Must be S3 URI or bytes."
        )

    # Resize and process
    return resize_image(image_data, target_width, target_height, allow_upscale)


def apply_adaptive_binarization(image_data: bytes) -> bytes:
    """
    Apply adaptive binarization using Pillow-only implementation.

    This preprocessing step can significantly improve OCR accuracy on documents with:
    - Uneven lighting or shadows
    - Low contrast text
    - Background noise or gradients

    Implements adaptive mean thresholding similar to OpenCV's ADAPTIVE_THRESH_MEAN_C
    with block_size=15 and C=10.

    Args:
        image_data: Raw image bytes

    Returns:
        Processed image as JPEG bytes with adaptive binarization applied
    """
    try:
        # Convert bytes to PIL Image
        pil_image = Image.open(io.BytesIO(image_data))

        # Convert to grayscale if not already
        if pil_image.mode != "L":
            pil_image = pil_image.convert("L")

        # Apply adaptive thresholding using Pillow operations
        block_size = 15
        C = 10

        # Create a blurred version for local mean calculation
        # Use BoxBlur with radius = block_size // 2 to approximate local mean
        radius = block_size // 2
        blurred = pil_image.filter(ImageFilter.BoxBlur(radius))

        # Apply adaptive threshold: original > (blurred - C) ? 255 : 0
        # Load pixel data for efficient access
        width, height = pil_image.size
        original_pixels = list(pil_image.getdata())
        blurred_pixels = list(blurred.getdata())

        binary_pixels = []
        # Apply thresholding pixel by pixel
        for orig, blur in zip(original_pixels, blurred_pixels):
            threshold = blur - C
            binary_pixels.append(255 if orig > threshold else 0)

        # Create binary image
        binary_image = Image.new("L", (width, height))
        binary_image.putdata(binary_pixels)

        # Convert to JPEG bytes
        img_byte_array = io.BytesIO()
        binary_image.save(img_byte_array, format="JPEG")

        logger.debug(
            "Applied adaptive binarization preprocessing (Pillow implementation)"
        )
        return img_byte_array.getvalue()

    except Exception as e:
        logger.error(f"Error applying adaptive binarization: {str(e)}")
        # Return original image if preprocessing fails
        logger.warning("Falling back to original image due to preprocessing error")
        return image_data


def prepare_bedrock_image_attachment(image_data: bytes) -> Dict[str, Any]:
    """
    Format an image for Bedrock API attachment.

    Every Bedrock image attachment in the pipeline (extraction, classification,
    assessment, OCR, few-shot examples) passes through here, so this is also
    where the per-image limit is enforced (#778): an image over the budget is
    downscaled to fit rather than failing the whole request. Callers that need
    the reduction recorded (extraction writes it to section metadata) call
    ``fit_image_to_bedrock_limit`` themselves first; the fit here is then a
    no-op pass-through.

    Args:
        image_data: Raw image bytes

    Returns:
        Formatted image attachment for Bedrock API
    """
    image_data, _fit = fit_image_to_bedrock_limit(image_data)
    detected_format = bedrock_image_format(image_data)
    logger.info(f"Detected image format: {detected_format}")
    return {"image": {"format": detected_format, "source": {"bytes": image_data}}}


def bedrock_image_format(image_data: bytes) -> str:
    """The Converse ``format`` string for these bytes ("jpeg", "png", …).

    Raises ``ValueError`` for anything Bedrock does not accept. Callers that
    re-encode an image (a fit can turn a PNG into a JPEG) must refresh the
    declared format from the new bytes, or Bedrock rejects the mismatch.
    """
    image = Image.open(io.BytesIO(image_data))
    format_mapping = {"JPEG": "jpeg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}
    detected_format = format_mapping.get(image.format or "")
    if not detected_format:
        raise ValueError(f"Unsupported image format: {image.format}")
    return detected_format
