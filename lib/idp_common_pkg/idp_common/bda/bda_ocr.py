# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Bedrock Data Automation (BDA) as a pure OCR engine.

BDA "standard output" for documents is a managed OCR product: given a page it
returns reading-order markdown (with tables and layout), plus word-level
bounding boxes and confidence. This module adapts that output to the same
Amazon Textract response shape the OCR service already consumes, so the rest of
the pipeline (``_parse_textract_response``, ``_generate_text_confidence_data``,
``_build_page_data``) works unchanged.

Two things about BDA standard output drive the design here (both verified
against the live service):

- **Sync ``InvokeDataAutomation`` is capped at ~10 pages.** The OCR service
  therefore invokes BDA one page at a time (feeding each already-rendered page
  image), which also gives per-page concurrency and retry isolation. So the
  converter here operates on the single-page standard output.
- **BDA line-level confidence is unreliable** (observed ~0.01 for every line),
  while word-level confidence is sound. We reconstruct each line's confidence as
  the mean of its child words' confidence.

BDA confidences are 0-1; Textract confidences are 0-100, so we scale by 100.
BDA bounding boxes are normalized 0-1 ({left, top, width, height}), matching
Textract's convention.

**Coordinate frame: BDA rectifies the page.** BDA internally deskews /
perspective-corrects the input image and returns every bounding box normalized
against that *rectified* image, not against the original page image the pipeline
stored (``image.jpg``) and the UI displays. On a clean, axis-aligned scan the
two frames coincide and boxes line up. On a skewed/rotated scan they don't, and
overlays land in the wrong spot. Each page's ``asset_metadata.corners`` gives
where the rectified image's four corners fall in the *original* image (0-1,
ordered TL, TR, BR, BL), so we map every box back into original-image space via
bilinear interpolation before emitting geometry. This makes BDA geometry agree
with Textract's (which is never rectified). The mapping is a no-op when corners
are absent or identity, preserving behavior for clean pages / older payloads.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Union

import boto3

logger = logging.getLogger(__name__)

# Suffix appended to the (sanitized) stack name to form the pure-OCR BDA
# project name. The project is now stack-scoped rather than account-global so
# that multiple stacks in one account do not share/interfere with a single
# project. BDA project names must match ``^[a-zA-Z0-9-_]+$`` (max 128 chars);
# underscores are allowed, so ``<stackname>_OCR_StdOutput`` is valid.
OCR_PROJECT_NAME_SUFFIX = "_OCR_StdOutput"

# Maximum length of a BDA project name.
_MAX_PROJECT_NAME_LEN = 128


def sanitize_ocr_project_name(stack_name: str) -> str:
    """Build the stack-scoped OCR project name from a stack name.

    The stack name is sanitized to the BDA-allowed character set (alphanumeric
    and hyphens) and truncated so that the ``_OCR_StdOutput`` suffix always
    survives within the 128-character project-name limit.
    """
    # Replace disallowed chars with hyphens and collapse runs of hyphens.
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", stack_name or "")
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    max_stack_len = _MAX_PROJECT_NAME_LEN - len(OCR_PROJECT_NAME_SUFFIX)
    sanitized = sanitized[:max_stack_len].strip("-")
    return f"{sanitized}{OCR_PROJECT_NAME_SUFFIX}"


# A "corners" quad that is (within tolerance) the unit square: the rectified
# image equals the original image, so no coordinate mapping is needed.
_IDENTITY_CORNERS = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
_CORNERS_IDENTITY_TOL = 1e-3


def _normalize_corners(
    corners: Optional[Any],
    scale: tuple[float, float] = (1.0, 1.0),
) -> Optional[List[tuple[float, float]]]:
    """Coerce BDA ``asset_metadata.corners`` to four (x, y) tuples, or None.

    Corners give the rectified image's four corners, ordered top-left, top-right,
    bottom-right, bottom-left. **BDA normalizes them against the rectified crop,
    not the original page**, so when BDA rectifies to a tight sub-region (e.g. a
    driver's license occupying part of a page) the raw values only span that
    fraction of the page. ``scale`` = (original_w / rectified_w,
    original_h / rectified_h) rescales them into original-image 0-1 space; pass
    (1, 1) when the frames already match (or the scale is unknown).

    Returns None when absent/malformed, or when the scaled quad is effectively
    the unit square (an identity rectification, so no mapping is required).
    """
    if not isinstance(corners, (list, tuple)) or len(corners) != 4:
        return None
    sx, sy = scale
    pts: List[tuple[float, float]] = []
    for c in corners:
        if isinstance(c, (list, tuple)) and len(c) == 2:
            try:
                pts.append((float(c[0]) * sx, float(c[1]) * sy))
            except (TypeError, ValueError):
                return None
        elif isinstance(c, dict) and "x" in c and "y" in c:
            try:
                pts.append((float(c["x"]) * sx, float(c["y"]) * sy))
            except (TypeError, ValueError):
                return None
        else:
            return None
    if all(
        abs(px - ix) <= _CORNERS_IDENTITY_TOL and abs(py - iy) <= _CORNERS_IDENTITY_TOL
        for (px, py), (ix, iy) in zip(pts, _IDENTITY_CORNERS)
    ):
        return None
    return pts


def _map_point(
    u: float, v: float, corners: List[tuple[float, float]]
) -> tuple[float, float]:
    """Bilinearly map a rectified-space point (u, v in 0-1) to original space.

    ``corners`` are the original-image coordinates of the rectified corners in
    TL, TR, BR, BL order. Bilinear interpolation across that quad inverts BDA's
    rectification closely enough for overlay purposes (BDA rectification is an
    affine/perspective deskew; bilinear is exact for affine and a good
    approximation for mild perspective).
    """
    (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = corners
    top_x = tlx + (trx - tlx) * u
    top_y = tly + (try_ - tly) * u
    bot_x = blx + (brx - blx) * u
    bot_y = bly + (bry - bly) * u
    return (top_x + (bot_x - top_x) * v, top_y + (bot_y - top_y) * v)


def _bbox_to_geometry(
    bbox: Optional[Dict[str, Any]],
    corners: Optional[List[tuple[float, float]]] = None,
) -> Optional[Dict[str, Any]]:
    """Convert a BDA bounding_box (0-1) to a Textract-format Geometry, or None.

    When ``corners`` is provided (a non-identity rectification quad), the box is
    mapped from BDA's rectified frame back into original-image space so it aligns
    with the stored page image / UI overlays. A rectified axis-aligned box maps
    to a (possibly non-rectangular) quadrilateral, which becomes the ``Polygon``;
    ``BoundingBox`` is that quad's axis-aligned envelope (Textract convention).
    """
    if not isinstance(bbox, dict):
        return None
    left = bbox.get("left", 0.0)
    top = bbox.get("top", 0.0)
    width = bbox.get("width", 0.0)
    height = bbox.get("height", 0.0)

    if corners is None:
        # No rectification (clean page / identity corners): pass through, and
        # synthesize a rectangular polygon so downstream geometry consumers (UI
        # highlighting) have the same shape Textract provides.
        polygon = [
            (left, top),
            (left + width, top),
            (left + width, top + height),
            (left, top + height),
        ]
    else:
        # Map each rectified corner back into original-image space. Order is
        # TL, TR, BR, BL to match Textract's polygon winding.
        polygon = [
            _map_point(left, top, corners),
            _map_point(left + width, top, corners),
            _map_point(left + width, top + height, corners),
            _map_point(left, top + height, corners),
        ]

    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return {
        "BoundingBox": {
            "Left": min_x,
            "Top": min_y,
            "Width": max_x - min_x,
            "Height": max_y - min_y,
        },
        "Polygon": [{"X": x, "Y": y} for x, y in polygon],
    }


def _first_bbox(unit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull the first bounding_box out of a BDA text_line / text_word.

    BDA sometimes serializes ``locations`` as a list of {page_index,
    bounding_box} and sometimes as a single object; tolerate both.
    """
    locations = unit.get("locations")
    if isinstance(locations, list):
        for loc in locations:
            if isinstance(loc, dict) and isinstance(loc.get("bounding_box"), dict):
                return loc["bounding_box"]
        return None
    if isinstance(locations, dict):
        bb = locations.get("bounding_box")
        return bb if isinstance(bb, dict) else None
    return None


def bda_standard_output_to_textract_blocks(
    standard_output: Union[str, Dict[str, Any]],
    page_index: Optional[int] = None,
    original_image_size: Optional[tuple[int, int]] = None,
) -> Dict[str, Any]:
    """Convert a BDA document standard-output payload to a Textract-format dict.

    Args:
        standard_output: The BDA ``standardOutput`` object (or its JSON string,
            as returned inline by the sync API). Expected keys include
            ``pages``, ``text_lines``, ``text_words``.
        page_index: If provided, only lines/words on this 0-based page index are
            included (used when a payload spans multiple pages). When None, all
            lines/words are included (the common single-page-invocation case).
        original_image_size: ``(width, height)`` in pixels of the original page
            image fed to BDA. Needed to correctly place boxes when BDA rectifies
            to a sub-region of the page: BDA's ``corners`` are normalized against
            the rectified crop (``asset_metadata.rectified_image_*_pixels``), so
            they must be rescaled by original/rectified to land in original-image
            space. When omitted, corners are assumed already in original-image
            space (correct for full-page rectification; may misplace boxes when
            BDA cropped to a sub-region).

    Returns:
        ``{"DocumentMetadata": {"Pages": 1}, "Blocks": [...]}`` with PAGE, LINE
        and WORD blocks. LINE confidence is the mean of its child WORD
        confidences; geometry and confidence are scaled to Textract conventions.
    """
    if isinstance(standard_output, str):
        standard_output = json.loads(standard_output)

    text_lines: List[Dict[str, Any]] = standard_output.get("text_lines", []) or []
    text_words: List[Dict[str, Any]] = standard_output.get("text_words", []) or []

    # Rectification corners are per-page (asset_metadata.corners). Map each
    # page_index to its normalized corners so every box can be re-projected into
    # original-image space; None means identity/absent (no mapping needed). BDA
    # normalizes corners against the *rectified* crop, so rescale by
    # original/rectified when we know both dimensions (see arg docstring).
    corners_by_page: Dict[Optional[int], Optional[List[tuple[float, float]]]] = {}
    for page in standard_output.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        asset_meta = page.get("asset_metadata") or {}
        scale = (1.0, 1.0)
        if original_image_size:
            rect_w = asset_meta.get("rectified_image_width_pixels")
            rect_h = asset_meta.get("rectified_image_height_pixels")
            orig_w, orig_h = original_image_size
            if (
                isinstance(rect_w, (int, float))
                and isinstance(rect_h, (int, float))
                and rect_w > 0
                and rect_h > 0
                and orig_w > 0
                and orig_h > 0
            ):
                scale = (orig_w / rect_w, orig_h / rect_h)
        corners_by_page[page.get("page_index")] = _normalize_corners(
            asset_meta.get("corners"), scale
        )

    def _corners_for(unit: Dict[str, Any]) -> Optional[List[tuple[float, float]]]:
        pi = unit.get("page_index")
        if pi in corners_by_page:
            return corners_by_page[pi]
        # Single-page invocations sometimes omit a matching page_index on the
        # unit; fall back to the sole page's corners when unambiguous.
        if len(corners_by_page) == 1:
            return next(iter(corners_by_page.values()))
        return None

    def _on_page(unit: Dict[str, Any]) -> bool:
        if page_index is None:
            return True
        return unit.get("page_index") == page_index

    # Index words by their parent line id so LINE -> WORD CHILD relationships and
    # per-line confidence averaging both work.
    words_by_line: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for word in text_words:
        if not _on_page(word):
            continue
        words_by_line.setdefault(word.get("line_id"), []).append(word)

    blocks: List[Dict[str, Any]] = []

    # A single synthetic PAGE block spanning the whole page.
    page_block_id = "page-0"
    page_block: Dict[str, Any] = {
        "BlockType": "PAGE",
        "Id": page_block_id,
        "Geometry": {
            "BoundingBox": {"Left": 0.0, "Top": 0.0, "Width": 1.0, "Height": 1.0},
            "Polygon": [
                {"X": 0.0, "Y": 0.0},
                {"X": 1.0, "Y": 0.0},
                {"X": 1.0, "Y": 1.0},
                {"X": 0.0, "Y": 1.0},
            ],
        },
        "Relationships": [],
    }
    blocks.append(page_block)
    page_child_ids: List[str] = []

    for line in text_lines:
        if not _on_page(line):
            continue
        line_id = line.get("id") or f"line-{len(blocks)}"
        child_words = words_by_line.get(line.get("id"), [])

        # Build WORD blocks first so we can average their confidence for the LINE.
        word_ids: List[str] = []
        word_blocks: List[Dict[str, Any]] = []
        word_confidences: List[float] = []
        for word in child_words:
            wconf = word.get("confidence")
            wid = word.get("id") or f"word-{len(blocks) + len(word_blocks)}"
            if isinstance(wconf, (int, float)):
                word_confidences.append(float(wconf))
                wconf_scaled = float(wconf) * 100.0
            else:
                wconf_scaled = 0.0
            word_blocks.append(
                {
                    "BlockType": "WORD",
                    "Id": wid,
                    "Text": word.get("text", ""),
                    "Confidence": wconf_scaled,
                    "Geometry": _bbox_to_geometry(
                        _first_bbox(word), _corners_for(word)
                    ),
                }
            )
            word_ids.append(wid)

        # LINE confidence = mean of child word confidences (BDA line confidence
        # is unreliable). Fall back to the line's own value only if no words.
        if word_confidences:
            line_conf = (sum(word_confidences) / len(word_confidences)) * 100.0
        else:
            lc = line.get("confidence")
            line_conf = float(lc) * 100.0 if isinstance(lc, (int, float)) else 0.0

        # BDA leaves ``text`` empty on table-cell lines (the content lives only in
        # ``text_words``). Synthesize the LINE text from its child words so the line
        # is not later discarded by the pageData builder, which drops empty-text
        # LINE blocks and would otherwise strip all table-cell text/confidence/
        # geometry for BDA-as-OCR runs.
        line_text = line.get("text", "")
        if not line_text and word_blocks:
            line_text = " ".join(wb["Text"] for wb in word_blocks if wb.get("Text"))

        line_block: Dict[str, Any] = {
            "BlockType": "LINE",
            "Id": line_id,
            "Text": line_text,
            "Confidence": line_conf,
            "Geometry": _bbox_to_geometry(_first_bbox(line), _corners_for(line)),
        }
        if word_ids:
            line_block["Relationships"] = [{"Type": "CHILD", "Ids": word_ids}]
        blocks.append(line_block)
        blocks.extend(word_blocks)
        page_child_ids.append(line_id)

    if page_child_ids:
        page_block["Relationships"] = [{"Type": "CHILD", "Ids": page_child_ids}]

    return {"DocumentMetadata": {"Pages": 1}, "Blocks": blocks}


def extract_markdown(
    standard_output: Union[str, Dict[str, Any]],
    page_index: Optional[int] = None,
) -> str:
    """Extract page markdown (preferred) or plain text from BDA standard output.

    Args:
        standard_output: BDA ``standardOutput`` object or JSON string.
        page_index: 0-based page to extract; None selects the first page present.

    Returns:
        The page's markdown representation, falling back to plain text, else "".
    """
    if isinstance(standard_output, str):
        standard_output = json.loads(standard_output)

    pages = standard_output.get("pages", []) or []
    target = None
    if page_index is None:
        target = pages[0] if pages else None
    else:
        for page in pages:
            if page.get("page_index") == page_index:
                target = page
                break

    if target is not None:
        rep = target.get("representation", {}) or {}
        return rep.get("markdown") or rep.get("text") or ""

    # No per-page representation: fall back to document-level text.
    doc_rep = (standard_output.get("document", {}) or {}).get(
        "representation", {}
    ) or {}
    return doc_rep.get("markdown") or doc_rep.get("text") or ""


# NOTE: The project-lifecycle helpers below (config builders, sanitizer,
# find/create/delete) are the canonical, unit-tested source, but they are
# *duplicated* — not imported — by the deploy-time custom-resource Lambda at
# ``src/lambda/bda_ocr_project/index.py`` (SAM's builder can't reach ``lib/`` at
# build time). A drift guard in that Lambda's tests asserts the two produce
# identical output; keep the copies in sync when changing either.


def build_ocr_project_standard_output_config() -> Dict[str, Any]:
    """Standard-output-only configuration for a pure-OCR BDA project.

    Suitable for a ``projectType="SYNC"`` project: exactly one text format
    (MARKDOWN), bounding boxes on, generative fields off (no LLM summaries),
    additional file formats off (sync emits none anyway).
    """
    return {
        "document": {
            "extraction": {
                "granularity": {"types": ["PAGE", "ELEMENT", "WORD", "LINE"]},
                "boundingBox": {"state": "ENABLED"},
            },
            "generativeField": {"state": "DISABLED"},
            "outputFormat": {
                "textFormat": {"types": ["MARKDOWN"]},
                "additionalFileFormat": {"state": "DISABLED"},
            },
        }
    }


def build_ocr_project_override_config() -> Dict[str, Any]:
    """Override configuration forcing image inputs to DOCUMENT modality.

    We feed BDA rendered page images (JPEG/PNG). Without routing overrides, BDA
    semantically classifies some page images as IMAGE (returning image analysis
    instead of document text extraction), producing empty OCR output. Forcing
    jpeg/png routing to DOCUMENT guarantees document text extraction.
    """
    return {"modalityRouting": {"jpeg": "DOCUMENT", "png": "DOCUMENT"}}


def _ensure_project_routing_override(client: Any, project_arn: str) -> None:
    """Ensure an existing OCR project has jpeg/png -> DOCUMENT modality routing.

    A project created by an earlier build may predate the routing override.
    Without it, rendered page images misclassify as IMAGE and produce empty
    OCR. If the override is missing or incomplete, update the project in place.
    """
    try:
        project = client.get_data_automation_project(projectArn=project_arn)["project"]
    except Exception:
        logger.warning(
            "Could not fetch BDA OCR project %s to verify routing", project_arn
        )
        return

    routing = (project.get("overrideConfiguration") or {}).get("modalityRouting") or {}
    if routing.get("jpeg") == "DOCUMENT" and routing.get("png") == "DOCUMENT":
        return

    logger.info(
        "Updating BDA OCR project %s to add jpeg/png->DOCUMENT modality routing",
        project_arn,
    )
    client.update_data_automation_project(
        projectArn=project_arn,
        standardOutputConfiguration=build_ocr_project_standard_output_config(),
        overrideConfiguration=build_ocr_project_override_config(),
    )


def _find_project_arn_by_name(client: Any, project_name: str) -> Optional[str]:
    """Return the ARN of the project with ``project_name``, or None."""
    try:
        paginator = client.get_paginator("list_data_automation_projects")
    except Exception:
        paginator = None

    if paginator is not None:
        for page in paginator.paginate():
            for proj in page.get("projects", []):
                if proj.get("projectName") == project_name:
                    return proj["projectArn"]
        return None

    for proj in client.list_data_automation_projects().get("projects", []):
        if proj.get("projectName") == project_name:
            return proj["projectArn"]
    return None


def find_or_create_ocr_project(
    project_name: str,
    region: Optional[str] = None,
    bda_control_client: Optional[Any] = None,
) -> str:
    """Find or create the stack-scoped pure-OCR standard-output SYNC project.

    Looks for a project named ``project_name``; creates it (projectType
    ``SYNC``, standard output only) if absent and waits for it to reach
    ``COMPLETED``. Returns the project ARN.

    This is a control-plane helper invoked at stack deploy time (by the BDA OCR
    project CloudFormation custom resource) — not on the OCR hot path.

    Args:
        project_name: The stack-scoped project name
            (see :func:`sanitize_ocr_project_name`).
        region: AWS region (defaults to the client/session region).
        bda_control_client: Optional pre-built ``bedrock-data-automation`` client.
    """
    client = bda_control_client or boto3.client(
        "bedrock-data-automation", region_name=region
    )

    # Reuse an existing project of this name if present.
    existing_arn = _find_project_arn_by_name(client, project_name)
    if existing_arn:
        # Verify the project carries the modality-routing override. A project
        # left over from an earlier build may lack it, which silently breaks OCR
        # (page images misroute to IMAGE -> empty text). Repair it in place.
        _ensure_project_routing_override(client, existing_arn)
        logger.info("Reusing BDA OCR project %s", existing_arn)
        return existing_arn

    logger.info("Creating BDA OCR project %s", project_name)
    try:
        resp = client.create_data_automation_project(
            projectName=project_name,
            projectDescription="GenAIIDP stack-scoped pure-OCR standard-output project",
            projectStage="LIVE",
            projectType="SYNC",
            standardOutputConfiguration=build_ocr_project_standard_output_config(),
            overrideConfiguration=build_ocr_project_override_config(),
        )
        project_arn = resp["projectArn"]
    except client.exceptions.ConflictException:
        # Another concurrent caller created it first; re-fetch by name and route
        # through the same routing-override repair check as the primary reuse path.
        logger.info("BDA OCR project already created concurrently; re-fetching")
        existing_arn = _find_project_arn_by_name(client, project_name)
        if existing_arn:
            _ensure_project_routing_override(client, existing_arn)
            return existing_arn
        raise

    # A freshly created project is IN_PROGRESS until provisioned.
    for _ in range(60):
        status = client.get_data_automation_project(projectArn=project_arn)["project"][
            "status"
        ]
        if status == "COMPLETED":
            break
        time.sleep(2)
    else:
        # Loop exhausted without COMPLETED: surface a diagnosable warning rather
        # than returning an ARN whose first invocation fails confusingly.
        logger.warning(
            "BDA OCR project %s not COMPLETED after ~120s (last status: %s); "
            "first invocations may fail until it finishes provisioning",
            project_arn,
            status,
        )
    return project_arn


def delete_ocr_project_by_name(
    project_name: str,
    region: Optional[str] = None,
    bda_control_client: Optional[Any] = None,
) -> Optional[str]:
    """Best-effort delete of the stack-scoped OCR project by name.

    Invoked by the CloudFormation custom resource on stack delete. Swallows a
    missing project (already deleted / never created) so stack deletion never
    fails. Returns the deleted project ARN, or None if nothing was deleted.
    """
    client = bda_control_client or boto3.client(
        "bedrock-data-automation", region_name=region
    )

    arn = _find_project_arn_by_name(client, project_name)
    if not arn:
        logger.info("BDA OCR project %s not found; nothing to delete", project_name)
        return None

    try:
        client.delete_data_automation_project(projectArn=arn)
        logger.info("Deleted BDA OCR project %s", arn)
        return arn
    except Exception:
        # Best-effort teardown: never let a delete failure block stack deletion.
        logger.warning("Failed to delete BDA OCR project %s", arn, exc_info=True)
        return None


# Geo prefixes used by BDA cross-region data-automation profiles. The naive
# region.split("-")[0] is wrong for Asia Pacific (ap-* -> "apac", not "ap") and
# for Canada: AWS names the Canada (Central) profile na.data-automation-v1 (it also
# covers ca-west-1 and three US regions); ca.data-automation-v1 is not a valid profile.
_PROFILE_GEO_PREFIXES = {
    "us": "us",
    "eu": "eu",
    "ap": "apac",
    "ca": "na",
    "sa": "sa",
}


def build_profile_arn(region: str, account_id: str, partition: str = "aws") -> str:
    """Construct the standard data-automation profile ARN for a region/account.

    Args:
        region: AWS region (e.g. ``us-west-2``, ``ap-southeast-2``).
        account_id: AWS account id.
        partition: AWS partition (``aws``, ``aws-us-gov``, ``aws-cn``); derived
            by the caller from the session so the ARN is partition-correct.
    """
    geo = _PROFILE_GEO_PREFIXES.get(region.split("-")[0], region.split("-")[0])
    return (
        f"arn:{partition}:bedrock:{region}:{account_id}:data-automation-profile/"
        f"{geo}.data-automation-v1"
    )
