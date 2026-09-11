// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * What a draft-labeling run may and may not claim on the run-level view.
 *
 * Such a run has no baseline by construction, so nothing is scored. The backend
 * still emits structural zeros, and the page printed them: "Classification: Page
 * Level Accuracy 0.000" and an empty "Documents with Lowest Weighted Overall
 * Scores" table, both directly below an alert saying there are no accuracy
 * metrics. A hard zero is a stronger and more wrong claim than N/A — it asserts
 * total failure where the honest answer is "not applicable".
 *
 * Same approach as VisualEditorModal.classGating: this is pure JSX gating inside a
 * component whose render needs the GraphQL client, settings and several parsed
 * AWSJSON blobs. Reading the source is blunt but it pins the decision.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'TestResults.tsx'), 'utf-8');

describe('TestResults draft-labeling gating', () => {
  it('does not render the lowest-scores table for a run that scored nothing', () => {
    expect(SOURCE).toMatch(/results\?\.weightedOverallScores && !results\.isDraftLabeling/);
  });

  it('withholds every accuracy input from the breakdown for a draft run', () => {
    for (const prop of ['accuracyBreakdown', 'splitClassificationMetrics', 'gradedPacketMetrics', 'fieldMetrics', 'averageWeightedScore']) {
      expect(SOURCE, `${prop} is not gated on isDraftLabeling`).toMatch(new RegExp(`${prop}=\\{results\\.isDraftLabeling \\? null :`));
    }
  });

  it('still shows the cost breakdown, which is real money either way', () => {
    // The one number a labeling run genuinely has. Gating it with the rest would
    // hide the spend on a 100-document labeling job.
    expect(SOURCE).toMatch(/costBreakdown=\{costBreakdown\}/);
  });

  it('tells the reader why there are no metrics rather than warning about it', () => {
    expect(SOURCE).toMatch(/produced labels rather than being scored/);
    expect(SOURCE).toMatch(/results\.status === 'COMPLETE' && results\.isDraftLabeling/);
  });
});
