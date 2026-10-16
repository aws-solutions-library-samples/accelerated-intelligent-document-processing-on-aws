// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Geometry for rotating a page in the page-image viewer.
 *
 * Three small numeric pieces, kept out of the component so they can be tested
 * directly: jsdom has no layout, so anything reading `offsetWidth` or a
 * transformed rect inside the component is unreachable from a test.
 *
 * All three share one convention: **rotation is clockwise degrees**, matching
 * CSS `rotate()`, and any angle is folded onto one of the four quarter turns.
 * Only multiples of 90 are produced by the viewer, but a caller is not stopped
 * from passing -90 or 450, and those must not disagree with 270 and 90.
 */

/** A bounding box in normalised (0–1) page coordinates. */
export interface NormalisedBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Fold any angle onto 0, 1, 2 or 3 clockwise quarter turns. */
const quarterTurns = (rotation: number): number => ((Math.round(rotation / 90) % 4) + 4) % 4;

/**
 * Map a bounding box from page space onto the rotated page.
 *
 * Overlays are absolutely positioned, axis-aligned divs measured against the
 * image's on-screen rect, and that rect already reports the *transposed*
 * extent of a quarter-turned image. A quarter turn also keeps an axis-aligned
 * box axis-aligned, so the whole correction is a corner remap plus a
 * width/height swap at 90° and 270° — without it a highlighted field lands on
 * an unrelated part of a rotated page.
 */
export const rotateBoundingBox = (bbox: NormalisedBox, rotation: number): NormalisedBox => {
  switch (quarterTurns(rotation)) {
    case 1: // 90° clockwise: the page's left edge becomes the top edge.
      return { left: 1 - (bbox.top + bbox.height), top: bbox.left, width: bbox.height, height: bbox.width };
    case 2:
      return {
        left: 1 - (bbox.left + bbox.width),
        top: 1 - (bbox.top + bbox.height),
        width: bbox.width,
        height: bbox.height,
      };
    case 3: // 270° clockwise: the page's top edge becomes the left edge.
      return { left: bbox.top, top: 1 - (bbox.left + bbox.width), width: bbox.height, height: bbox.width };
    default:
      return bbox;
  }
};

/**
 * Turn an offset measured from the page centre, in screen pixels.
 *
 * Spelled as exact quarter turns rather than `Math.cos`/`Math.sin`, which give
 * 6.1e-17 instead of 0 for a right angle. The direction matches
 * {@link rotateBoundingBox}: at 90° an offset pointing up-left (the page's
 * top-left corner) comes to point up-right.
 */
export const rotateOffset = (dx: number, dy: number, rotation: number): { x: number; y: number } => {
  switch (quarterTurns(rotation)) {
    case 1:
      return { x: -dy, y: dx };
    case 2:
      return { x: -dx, y: -dy };
    case 3:
      return { x: dy, y: -dx };
    default:
      return { x: dx, y: dy };
  }
};

interface RotationFitInput {
  /** The image's *layout* box — unaffected by the transform being computed. */
  imageWidth: number;
  imageHeight: number;
  containerWidth: number;
  containerHeight: number;
  rotation: number;
}

/**
 * The extra scale that keeps a quarter-turned page inside its container.
 *
 * The browser fits the image to the container with `maxWidth`/`maxHeight`, so
 * at 90°/270° its on-screen footprint is that layout box **transposed** — which
 * overflows any container that is not square, and the viewer clips rather than
 * scrolls. The ground-truth editor is a half-width column, so a portrait page
 * rotated there would lose a third of its width. This is what makes "rotate"
 * mean "rotate and still fit" instead of "rotate and crop".
 *
 * Returns `null` when nothing is measurable yet — a container with no layout,
 * or an image that has not loaded. The caller keeps its last good value rather
 * than taking a zero, which would scale the page to nothing and (since the pan
 * offset is divided by the scale) blank the viewer.
 */
export const rotationFitScale = ({
  imageWidth,
  imageHeight,
  containerWidth,
  containerHeight,
  rotation,
}: RotationFitInput): number | null => {
  // At 0°/180° the footprint is the layout box, which already fits. True
  // without measuring anything, so it answers before the guard below.
  if (quarterTurns(rotation) % 2 === 0) return 1;
  if (!imageWidth || !imageHeight || !containerWidth || !containerHeight) return null;
  if (![imageWidth, imageHeight, containerWidth, containerHeight].every(Number.isFinite)) return null;
  // Clamped at 1 so that rotating a page never changes how big it appears when
  // it already fits: the rotated footprint of a *large* page could legitimately
  // be scaled up and still sit within its natural resolution, but growing on
  // rotate is a jump, and a small page would jump the most.
  const fit = Math.min(containerWidth / imageHeight, containerHeight / imageWidth, 1);
  return Number.isFinite(fit) && fit > 0 ? fit : null;
};
