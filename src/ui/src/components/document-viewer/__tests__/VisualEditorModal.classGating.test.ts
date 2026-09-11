// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The class comparison must not be hidden behind the Show Evaluation toggle.
 *
 * The comparison itself is covered by ClassMismatchIndicator.test.tsx. What is
 * asserted here is the modal's *gating decision*, which is where the feature was
 * lost: `SectionClassEvaluation` was rendered inside a `showEvaluation && baselineData`
 * block, and that toggle defaults to off and only appears once the evaluation status
 * resolves. A reviewer opened View Data on a document that had a baseline, saw no
 * class comparison, and reasonably concluded it had never been built.
 *
 * Rendering the whole modal to assert this would mean mocking the GraphQL client,
 * settings, and the S3 fetch, for a one-line JSX condition. Reading the source is a
 * blunter instrument but it pins the decision and the reason, which is what a future
 * change needs to see. The baseline is fetched whenever one exists — the effect that
 * loads it does not consult the toggle — so rendering this costs nothing.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'VisualEditorModal.tsx'), 'utf-8');

/** The JSX guard immediately preceding a component's render site. */
const guardBefore = (component: string): string => {
  const at = SOURCE.indexOf(`<${component}`);
  expect(at, `${component} is not rendered in VisualEditorModal`).toBeGreaterThan(-1);
  // Walk back to the nearest opening brace expression, which is the JSX condition.
  const from = SOURCE.lastIndexOf('{', at);
  return SOURCE.slice(from, at);
};

describe('VisualEditorModal class-comparison gating', () => {
  it('renders the class comparison whenever a baseline is loaded', () => {
    expect(guardBefore('SectionClassEvaluation')).toMatch(/baselineData\s*&&/);
  });

  it('does not require the Show Evaluation toggle for the class comparison', () => {
    // The regression: `showEvaluation && baselineData && (<SectionClassEvaluation .../>)`.
    expect(guardBefore('SectionClassEvaluation')).not.toMatch(/showEvaluation/);
  });

  it('still gates the per-field comparison behind the toggle', () => {
    // Per-field scores and reasons are the noisy part and stay opt-in; only the
    // one-line class verdict is unconditional. Without this, "ungate the class"
    // could be satisfied by ungating everything.
    expect(SOURCE).toMatch(/showEvaluation && baselineData/);
    expect(SOURCE).toMatch(/showComparison=\{showEvaluation\}/);
  });

  it('tells the reader what the toggle would add when a baseline exists but is off', () => {
    // The toggle was the only clue the capability existed, and it renders late.
    expect(SOURCE).toMatch(/baselineData && !showEvaluation/);
    expect(SOURCE).toMatch(/has an evaluation baseline/);
  });
});
