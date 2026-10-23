// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The field pane gets its height from its parent, never from a figure.
 *
 * Reported while validating [#1327]: the field list stopped short of its last
 * rows, and the pane's own bottom border was not visible either — which on a
 * document whose final rows are nested list items reads as a missing control
 * rather than as a cut-off view.
 *
 * Both symptoms are one cause. The row holding the two panes has a definite
 * height and clips with `overflow: hidden`. A Cloudscape `Container` sat between
 * that row and the scrolling list, and `Container` takes its height from its
 * content — so the list could only be sized by naming a figure, and wherever the
 * header plus that figure exceeded the row, everything past it (the end of the
 * list, and the border below it) fell outside the visible area. Naming a *better*
 * figure does not fix that; it moves the viewport at which it happens.
 *
 * The fix is the pattern the pages pane beside it already uses: a plain bordered
 * flex column, with the scrolling box as `flex: 1; minHeight: 0` and no height of
 * its own. `minHeight: 0` is the load-bearing half — a flex item's default
 * `min-height: auto` refuses to shrink below its content, which is exactly how a
 * box meant to scroll ends up taller than the space it has.
 *
 * ⚠️ **This test cannot prove the pane scrolls or that the border is visible.**
 * jsdom performs no layout: every computed height, `scrollHeight` and
 * `clientHeight` there is zero, so an assertion about reachability passes against
 * the broken pane too. What it pins is that the height stays delegated to the
 * parent rather than reintroduced as a number — the shape of the defect, not its
 * symptom. Confirming the behaviour needs a browser: the `ux-test` tier, or an eye
 * on a deployed stack at more than one window height.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'VisualEditorModal.tsx'), 'utf-8');

/**
 * The whole right-hand pane: its outer div, the bordered box, the header and the
 * scrolling box's style, up to the field renderer it wraps.
 *
 * ⚠️ Anchored on the pane marker rather than on `overflowY: 'auto'`. A style
 * object is unordered, so a height written *above* that property sits outside a
 * window that starts at it — and a revert-check proved these assertions passed
 * against a reintroduced `maxHeight: '550px'` placed one line earlier. A test
 * that cannot see the regression it names is worse than no test.
 */
const FIELD_PANE = SOURCE.slice(SOURCE.indexOf('Right side - Form fields'), SOURCE.indexOf('<FormFieldRenderer'));

describe("the field pane's height", () => {
  it('delegates to the parent instead of naming a figure', () => {
    // `flex: 1` is only meaningful with `minHeight: 0`; together they are what
    // makes the box take the space left after the header and no more.
    expect(FIELD_PANE).toContain("overflowY: 'auto'");
    expect(FIELD_PANE).toContain('minHeight: 0');
    // No height anywhere in the pane: not on the bordered box, not on the
    // scrolling box, wherever in its style object it might be written.
    expect(FIELD_PANE).not.toMatch(/maxHeight/);
  });

  it('does not reintroduce a height for the scrolling box', () => {
    // The original defect was a literal; the first attempt at fixing it was an
    // arithmetic expression derived from the row. Both name a figure, and both
    // fail at some viewport size. Neither belongs here.
    expect(FIELD_PANE).not.toMatch(/maxHeight:\s*'?\d+px/);
    expect(FIELD_PANE).not.toMatch(/maxHeight:\s*`?min\(calc/);
    expect(FIELD_PANE).not.toMatch(/minHeight:\s*'?\d+px/);
  });

  it('is a flex column in a pane of definite height, which is what flex: 1 needs', () => {
    // Without this chain `flex: 1` resolves against nothing — the state the
    // Cloudscape Container left it in, and the reason a figure was needed at all.
    expect(FIELD_PANE).toContain("height: '100%'");
    expect(FIELD_PANE).toContain("overflow: 'hidden'");
    expect(FIELD_PANE).toContain("flexDirection: 'column'");
    // And no Container between them, which is what broke the chain.
    expect(FIELD_PANE).not.toContain('<Container');
  });

  it('does not let the content refuse to shrink inside the scrolling box', () => {
    // `minHeight: 'fit-content'` on the box's own child is the same
    // refusal-to-shrink one level down, and it kept the content taller than the
    // box that is meant to scroll it.
    //
    // Matched as the attribute, not as the phrase: the source names it in a
    // comment saying why it is absent, and a substring check cannot tell the two
    // apart — so this would fail against the fixed file.
    expect(FIELD_PANE).not.toMatch(/style=\{\{\s*minHeight: 'fit-content'/);
  });
});
