// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A page can be rotated in the viewer, and the overlays rotate with it.
 *
 * Scanned pages arrive sideways, which made them unreadable in the Test Studio
 * ground-truth editor — the viewer offered zoom and pan but no rotation
 * (issue #1320).
 *
 * The geometry is the part worth pinning. Bounding-box overlays are
 * axis-aligned divs positioned from the image's on-screen rect, and that rect
 * reports a quarter-turned image's extent already transposed. So the overlay
 * needs its own corner remap, and an off-by-one-quarter-turn there is not
 * visible in a diff: it puts a highlighted field somewhere plausible but wrong
 * on the page. The invariants below (corner mapping, extent swap, four turns
 * returning to the start, staying inside the page) are what distinguish the
 * four cases from each other.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

vi.mock('../generate-s3-presigned-url', () => ({ default: vi.fn(() => Promise.resolve('https://example.test/signed')) }));
// Stable identities on purpose: the viewer's image-load effect is keyed on
// these, so a fresh object per render re-enters loading forever.
const APP_CONTEXT = { currentCredentials: {} };
const VERSION_CONTEXT = { isHistorical: false, runId: null, versionIdForUri: () => undefined };
vi.mock('../../../contexts/app', () => ({ default: () => APP_CONTEXT }));
vi.mock('../../../contexts/document-version', () => ({ useDocumentVersion: () => VERSION_CONTEXT }));

import PageImageViewer from '../PageImageViewer';
import { rotateBoundingBox, rotateOffset, rotationFitScale } from '../page-rotation-utils';
import type { NormalisedBox } from '../page-rotation-utils';

/** A box in the page's upper-left quadrant, deliberately not square. */
const BOX = { left: 0.1, top: 0.2, width: 0.3, height: 0.4 };

/**
 * Compare boxes approximately: the remap subtracts normalised sums, so exact
 * equality would be asserting against float noise (0.2 + 0.4 is not 0.6) for a
 * difference far below one device pixel.
 */
const expectBox = (actual: NormalisedBox, expected: NormalisedBox) => {
  (['left', 'top', 'width', 'height'] as const).forEach((key) => {
    expect(actual[key]).toBeCloseTo(expected[key], 10);
  });
};

describe('rotateBoundingBox', () => {
  it('leaves the box alone when the page is not rotated', () => {
    expect(rotateBoundingBox(BOX, 0)).toEqual(BOX);
    expect(rotateBoundingBox(BOX, 360)).toEqual(BOX);
  });

  it('sends the top-left corner of the page to the top-right at 90°', () => {
    // A strip along the page's top-left corner...
    const corner = { left: 0, top: 0, width: 0.2, height: 0.1 };
    // ...ends up against the right edge, running down from the top.
    expect(rotateBoundingBox(corner, 90)).toEqual({ left: 0.9, top: 0, width: 0.1, height: 0.2 });
  });

  it('sends the top-left corner of the page to the bottom-left at 270°', () => {
    const corner = { left: 0, top: 0, width: 0.2, height: 0.1 };
    expect(rotateBoundingBox(corner, 270)).toEqual({ left: 0, top: 0.8, width: 0.1, height: 0.2 });
  });

  it('transposes the extent on a quarter turn and preserves it on a half turn', () => {
    expect(rotateBoundingBox(BOX, 90)).toMatchObject({ width: BOX.height, height: BOX.width });
    expect(rotateBoundingBox(BOX, 270)).toMatchObject({ width: BOX.height, height: BOX.width });
    expect(rotateBoundingBox(BOX, 180)).toMatchObject({ width: BOX.width, height: BOX.height });
  });

  it('reflects the box through the page centre at 180°', () => {
    expectBox(rotateBoundingBox(BOX, 180), { left: 0.6, top: 0.4, width: 0.3, height: 0.4 });
  });

  it('distinguishes all four quarter turns', () => {
    const seen = [0, 90, 180, 270].map((deg) => JSON.stringify(rotateBoundingBox(BOX, deg)));
    expect(new Set(seen).size).toBe(4);
  });

  it('returns to the original box after four right turns', () => {
    let box = BOX;
    for (let i = 0; i < 4; i += 1) box = rotateBoundingBox(box, 90);
    expectBox(box, BOX);
  });

  it('composes: two right turns are a half turn, three are a left turn', () => {
    expectBox(rotateBoundingBox(rotateBoundingBox(BOX, 90), 90), rotateBoundingBox(BOX, 180));
    expectBox(rotateBoundingBox(rotateBoundingBox(BOX, 180), 90), rotateBoundingBox(BOX, 270));
  });

  it('reads a left turn and its negative spelling the same way', () => {
    // The viewer keeps rotation in 0–359, but nothing stops a caller passing
    // -90, and the two must not disagree.
    expectBox(rotateBoundingBox(BOX, -90), rotateBoundingBox(BOX, 270));
    expectBox(rotateBoundingBox(BOX, -360), BOX);
    expectBox(rotateBoundingBox(BOX, 450), rotateBoundingBox(BOX, 90));
  });

  it('agrees with a pixel-level rotation of the page', () => {
    // An independent model of the same thing, to catch a reversed direction —
    // the failure mode the cases above cannot see, since they encode the same
    // assumption the implementation does. Rotate the page's *pixels* instead:
    // on a clockwise quarter turn a pixel at (x, y) of a W×H page lands at
    // (H - y, x) of the H×W result. Then renormalise and compare.
    const W = 400;
    const H = 600;
    // Page dimensions are a parameter because the turn swaps them: chaining two
    // turns has to feed the second one an H×W page.
    const rotateByPixels = (b: NormalisedBox, w: number, h: number): NormalisedBox => {
      const x1 = b.left * w;
      const x2 = (b.left + b.width) * w;
      const y1 = b.top * h;
      const y2 = (b.top + b.height) * h;
      // Corners after the turn, in the rotated page's pixel space (h wide, w tall).
      const left = h - y2;
      const right = h - y1;
      return { left: left / h, top: x1 / w, width: (right - left) / h, height: (x2 - x1) / w };
    };

    // A fixed spread of boxes: corners, edges, a thin strip, the whole page.
    const boxes: NormalisedBox[] = [
      BOX,
      { left: 0, top: 0, width: 1, height: 1 },
      { left: 0, top: 0, width: 0.1, height: 0.1 },
      { left: 0.9, top: 0.9, width: 0.1, height: 0.1 },
      { left: 0.25, top: 0, width: 0.5, height: 0.02 },
      { left: 0.98, top: 0.4, width: 0.02, height: 0.2 },
    ];
    boxes.forEach((b) => expectBox(rotateBoundingBox(b, 90), rotateByPixels(b, W, H)));
    // Two pixel-level right turns must likewise agree with the half turn, and
    // three with the left turn.
    boxes.forEach((b) => expectBox(rotateBoundingBox(b, 180), rotateByPixels(rotateByPixels(b, W, H), H, W)));
    boxes.forEach((b) => expectBox(rotateBoundingBox(b, 270), rotateByPixels(rotateByPixels(rotateByPixels(b, W, H), H, W), W, H)));
  });

  it('keeps the box inside the page at every rotation', () => {
    [0, 90, 180, 270].forEach((deg) => {
      const r = rotateBoundingBox(BOX, deg);
      expect(r.left).toBeGreaterThanOrEqual(0);
      expect(r.top).toBeGreaterThanOrEqual(0);
      expect(r.left + r.width).toBeLessThanOrEqual(1);
      expect(r.top + r.height).toBeLessThanOrEqual(1);
    });
  });
});

const ONE_PAGE_IDS = ['1'];
const ONE_PAGE = [{ Id: '1', ImageUri: 'blob:page-1' }];
const TWO_PAGE_IDS = ['1', '2'];
const TWO_PAGES = [
  { Id: '1', ImageUri: 'blob:page-1' },
  { Id: '2', ImageUri: 'blob:page-2' },
];

describe('rotationFitScale', () => {
  // A portrait page fitted into the Test Studio editor's half-width column:
  // the shape that motivated re-fitting at all.
  const PORTRAIT = { imageWidth: 400, imageHeight: 517, containerWidth: 400, containerHeight: 700 };

  it('leaves an unrotated page alone, measurable or not', () => {
    expect(rotationFitScale({ ...PORTRAIT, rotation: 0 })).toBe(1);
    expect(rotationFitScale({ ...PORTRAIT, rotation: 180 })).toBe(1);
    expect(rotationFitScale({ ...PORTRAIT, rotation: 360 })).toBe(1);
    // No layout yet is still an answer at 0°, because nothing needs measuring.
    expect(rotationFitScale({ imageWidth: 0, imageHeight: 0, containerWidth: 0, containerHeight: 0, rotation: 0 })).toBe(1);
  });

  it('shrinks a quarter-turned portrait page enough to clear the container', () => {
    const fit = rotationFitScale({ ...PORTRAIT, rotation: 90 });
    expect(fit).not.toBeNull();
    // The footprint is the layout box transposed: 517 wide in a 400 container.
    expect(fit).toBeCloseTo(400 / 517, 10);
    // The property that matters, stated independently of the number above:
    // after scaling, the rotated footprint fits in both axes.
    const scaled = fit as number;
    expect(PORTRAIT.imageHeight * scaled).toBeLessThanOrEqual(PORTRAIT.containerWidth + 1e-9);
    expect(PORTRAIT.imageWidth * scaled).toBeLessThanOrEqual(PORTRAIT.containerHeight + 1e-9);
  });

  it('treats 270° the same as 90°, and negative and wrapped angles too', () => {
    const at90 = rotationFitScale({ ...PORTRAIT, rotation: 90 });
    expect(rotationFitScale({ ...PORTRAIT, rotation: 270 })).toBe(at90);
    expect(rotationFitScale({ ...PORTRAIT, rotation: -90 })).toBe(at90);
    expect(rotationFitScale({ ...PORTRAIT, rotation: 450 })).toBe(at90);
  });

  it('never magnifies a page that already fits after turning', () => {
    // Landscape page in a tall container: the rotated footprint has room to
    // grow, and must not take it — rotating is not a zoom gesture.
    const landscape = { imageWidth: 400, imageHeight: 200, containerWidth: 400, containerHeight: 700 };
    expect(rotationFitScale({ ...landscape, rotation: 90 })).toBe(1);
    // A square page in a square container is the exactly-fits boundary.
    expect(rotationFitScale({ imageWidth: 300, imageHeight: 300, containerWidth: 300, containerHeight: 300, rotation: 90 })).toBe(1);
  });

  it('refuses to answer rather than returning a zero scale', () => {
    // The caller keeps its last good value on null. A 0 would scale the page
    // out of existence and, since pan is divided by the scale, blank the view.
    const unmeasurable = [
      { imageWidth: 0, imageHeight: 517, containerWidth: 400, containerHeight: 700 },
      { imageWidth: 400, imageHeight: 0, containerWidth: 400, containerHeight: 700 },
      // A hidden or not-yet-laid-out container, which is the live case.
      { imageWidth: 400, imageHeight: 517, containerWidth: 0, containerHeight: 700 },
      { imageWidth: 400, imageHeight: 517, containerWidth: 400, containerHeight: 0 },
      { imageWidth: Number.NaN, imageHeight: 517, containerWidth: 400, containerHeight: 700 },
      { imageWidth: 400, imageHeight: Number.POSITIVE_INFINITY, containerWidth: 400, containerHeight: 700 },
    ];
    unmeasurable.forEach((dims) => expect(rotationFitScale({ ...dims, rotation: 90 })).toBeNull());
  });
});

describe('rotateOffset', () => {
  it('turns an offset clockwise, exactly', () => {
    // Pointing right becomes pointing down: y grows downward on screen, which
    // is what makes a positive CSS angle read as clockwise.
    expect(rotateOffset(1, 0, 90)).toEqual({ x: -0, y: 1 });
    expect(rotateOffset(1, 0, 180)).toEqual({ x: -1, y: -0 });
    expect(rotateOffset(1, 0, 270)).toEqual({ x: 0, y: -1 });
    expect(rotateOffset(1, 0, 0)).toEqual({ x: 1, y: 0 });
  });

  it('agrees with the trigonometric form it replaced', () => {
    // Why it is not written with cos/sin: that gives 6.1e-17 for a right
    // angle. This pins the two against each other so the exact form cannot
    // drift from the rotation everything else assumes.
    const byTrig = (dx: number, dy: number, deg: number) => {
      const r = (deg * Math.PI) / 180;
      return { x: dx * Math.cos(r) - dy * Math.sin(r), y: dx * Math.sin(r) + dy * Math.cos(r) };
    };
    [
      [3, 7],
      [-120, 45],
      [0, -18],
    ].forEach(([dx, dy]) => {
      [0, 90, 180, 270].forEach((deg) => {
        const exact = rotateOffset(dx, dy, deg);
        const trig = byTrig(dx, dy, deg);
        expect(exact.x).toBeCloseTo(trig.x, 9);
        expect(exact.y).toBeCloseTo(trig.y, 9);
      });
    });
  });

  it('turns in the same direction as the bounding-box remap', () => {
    // The two must agree or a zoom-to-field would centre somewhere the
    // highlight is not. The page's top-left corner sits up and to the left of
    // the centre; a right turn must bring it up and to the right.
    const corner = rotateOffset(-1, -1, 90);
    expect(corner.x).toBeGreaterThan(0);
    expect(corner.y).toBeLessThan(0);
    const box = rotateBoundingBox({ left: 0, top: 0, width: 0.1, height: 0.1 }, 90);
    expect(box.left).toBeGreaterThan(0.5); // moved to the right half
    expect(box.top).toBeLessThan(0.5); // stayed in the top half
  });

  it('returns to the original offset after four right turns', () => {
    let v = { x: 13, y: -29 };
    for (let i = 0; i < 4; i += 1) v = rotateOffset(v.x, v.y, 90);
    expect(v).toEqual({ x: 13, y: -29 });
  });
});

/** Render the viewer over one client-side page image (no presigning needed). */
const renderViewer = () => render(<PageImageViewer pageIds={ONE_PAGE_IDS} documentPages={ONE_PAGE} />);

const pageImage = () => screen.getByAltText('Page 1');

describe('PageImageViewer rotation controls', () => {
  it('names the rotate controls for a screen reader rather than leaving a glyph', async () => {
    // Their content is "↺"/"↻", which a screen reader reads as "anticlockwise
    // open circle arrow" or skips. `title` alone is a fallback and is not
    // announced dependably, so the name is asserted here — the queries in the
    // tests below would pass on `title` alone and would not catch its loss.
    renderViewer();
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    ['Rotate left 90 degrees', 'Rotate right 90 degrees'].forEach((name) => {
      const control = screen.getByRole('button', { name });
      expect(control).toHaveAttribute('aria-label', name);
    });
  });

  it('turns the page right, wrapping back to 0° on the fourth click', async () => {
    renderViewer();
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    const right = screen.getByRole('button', { name: 'Rotate right 90 degrees' });
    expect(screen.getByText('0°')).toBeInTheDocument();

    right.click();
    await waitFor(() => expect(screen.getByText('90°')).toBeInTheDocument());
    expect(pageImage().style.transform).toContain('rotate(90deg)');

    right.click();
    right.click();
    await waitFor(() => expect(screen.getByText('270°')).toBeInTheDocument());

    right.click();
    await waitFor(() => expect(screen.getByText('0°')).toBeInTheDocument());
  });

  it('turns the page left without ever showing a negative angle', async () => {
    renderViewer();
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    screen.getByRole('button', { name: 'Rotate left 90 degrees' }).click();
    await waitFor(() => expect(screen.getByText('270°')).toBeInTheDocument());
    expect(screen.queryByText('-90°')).not.toBeInTheDocument();
  });

  it('clears the rotation on reset, along with zoom and pan', async () => {
    renderViewer();
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    screen.getByRole('button', { name: 'Rotate right 90 degrees' }).click();
    screen.getByTitle('Zoom In').click();
    await waitFor(() => expect(screen.getByText('90°')).toBeInTheDocument());

    screen.getByTitle('Reset View').click();
    await waitFor(() => expect(screen.getByText('0°')).toBeInTheDocument());
    expect(screen.getByText('100%')).toBeInTheDocument();
  });

  it('keeps the rotation applied while navigating pages', async () => {
    // A misoriented scan is usually misoriented on every page, so rotation is
    // sticky across page navigation the way zoom and pan already are.
    //
    // Sticky *within one mounted viewer*, which is as far as this can go: a
    // parent that re-keys the viewer per page resets it along with zoom and
    // pan, and PageTextEditorModal does exactly that to re-seed `initialPage`.
    // So do not read this as a promise about every screen.
    render(<PageImageViewer pageIds={TWO_PAGE_IDS} documentPages={TWO_PAGES} />);
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    screen.getByRole('button', { name: 'Rotate right 90 degrees' }).click();
    await waitFor(() => expect(screen.getByText('90°')).toBeInTheDocument());

    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();

    screen.getByRole('button', { name: 'Next page' }).click();

    await waitFor(() => expect(screen.getByText('Page 2 of 2')).toBeInTheDocument());
    expect(screen.getByText('90°')).toBeInTheDocument();
    expect(screen.getByAltText('Page 2').style.transform).toContain('rotate(90deg)');
  });
});
