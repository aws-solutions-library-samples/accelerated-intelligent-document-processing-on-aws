# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import io
import logging
import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple, Union

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
# Claude rejects any image over 8,000 px on a side regardless of byte size.
BEDROCK_IMAGE_MAX_DIMENSION = 8000
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
    data = image_data
    fmt = original_format
    for passes in range(1, _FIT_MAX_PASSES + 1):
        # Bytes scale roughly with pixel area, so the linear factor is the
        # square root of the size ratio; the dimension cap is linear. Always
        # shrink by at least a little so the loop cannot stall.
        size_ratio = math.sqrt(max_encoded_bytes * _FIT_TARGET_FRACTION / encoded)
        dim_ratio = max_dimension / max(width, height)
        scale = min(size_ratio, dim_ratio, _FIT_MAX_STEP_RATIO)
        new_w = max(1, int(width * scale))
        new_h = max(1, int(height * scale))

        use_jpeg = fmt == "JPEG" or passes > _FIT_NATIVE_FORMAT_PASSES
        save_fmt = "JPEG" if use_jpeg else fmt
        if save_fmt not in ("JPEG", "PNG", "GIF", "WEBP"):
            save_fmt = "JPEG"  # Bedrock accepts only these four anyway

        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
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
        elif save_fmt == "PNG":
            save_kwargs.update(optimize=True)
        resized.save(buf, **save_kwargs)

        data = buf.getvalue()
        encoded = base64_encoded_size(len(data))
        img, width, height, fmt = resized, new_w, new_h, save_fmt
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
            logger.warning(
                "Page image exceeded Bedrock's per-image limit (%s); downscaled "
                "%dx%d %s (%s B) -> %dx%d %s (%s B, %s B encoded) in %d pass(es) "
                "so the request can succeed. Set extraction/classification "
                "image.target_width/target_height to avoid this per request.",
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
    # Detect image format from image data
    image = Image.open(io.BytesIO(image_data))
    format_mapping = {"JPEG": "jpeg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}
    detected_format = format_mapping.get(image.format)
    if not detected_format:
        raise ValueError(f"Unsupported image format: {image.format}")
    logger.info(f"Detected image format: {detected_format}")
    return {"image": {"format": detected_format, "source": {"bytes": image_data}}}
