// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Two things found by working the annotate screen as an Annotator on a live stack,
 * neither of which unit tests could have surfaced because both are about what the
 * screen offers next, not about what a function returns.
 *
 * 1. Reviewing a document drops it out of the queue. The only route back was the
 *    action on the "Queue complete" alert, which by definition appears once every
 *    OTHER document is done. Searching the document by name before then returned
 *    "0 matches" and "No documents to review", with nothing saying it was being
 *    filtered out — the exact state an annotator who has just mis-saved is in.
 *
 * 2. The accuracy estimate was printed to 0.1% at every confidence tier. One review
 *    of a twelve-document set moved a displayed 85.1% to 92.2%: a seven-point swing
 *    under a digit claiming tenths. EstimateConfidence exists precisely so "a
 *    cold-start estimate is never rendered with the same authority as a measured
 *    one", and the headline number was the one place ignoring it.
 *
 * Asserted at source level, following the sibling editGating and versioning tests.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const SOURCE = readFileSync(join(HERE, 'AnnotationWorkspace.tsx'), 'utf-8');
const TIER = readFileSync(join(HERE, 'TestSetDetail.tsx'), 'utf-8');
const CURVE = readFileSync(
  join(HERE, '..', '..', '..', '..', '..', 'lib', 'idp_common_pkg', 'idp_common', 'evaluation', 'confidence_curve.py'),
  'utf-8',
);

describe('reaching a document that has already been reviewed', () => {
  it('offers the reveal while there is still other work, not only once the queue empties', () => {
    // The regression is positional: this control existed, but only as the `action` of
    // the "Queue complete" alert. Rendering it in the rail is what makes it reachable
    // at the moment it is needed.
    const rail = SOURCE.slice(SOURCE.indexOf('filteringPlaceholder="Find a document"'));
    expect(rail).toMatch(/\{!showReviewed && \(\s*<Button variant="inline-link" onClick=\{\(\) => setShowReviewed\(true\)\}/);
  });

  it('does not claim there is nothing to review when a filter is what emptied the list', () => {
    // "No documents to review." is false in that state: the documents are there, the
    // text just does not match one of them.
    expect(SOURCE).toMatch(/queueFilter \? `No document matches "\$\{queueFilter\}"\.` : 'No documents to review\.'/);
  });

  it('says why a search can come up empty, since the likeliest reason is hidden by default', () => {
    // The whole sentence, not just "Reviewed documents are hidden": that phrase already
    // existed in this file as a comment explaining the default, so the looser pattern
    // passed against the unfixed source. Caught by revert-checking.
    expect(SOURCE).toMatch(/Reviewed documents are hidden — show them to search those too\./);
  });
});

describe('how precisely the label-accuracy estimate is printed', () => {
  it('spends a decimal place only on a curve measured on this set', () => {
    expect(SOURCE).toMatch(/const estimateDecimals = impact\?\.estimateConfidence === 'measured' \? 1 : 0;/);
  });

  it('applies it to both the headline and the projection under it', () => {
    // The projection is the same estimate one step on; printing it to a different
    // precision would make the pair read as two different kinds of number.
    expect(SOURCE).toMatch(/formatPct\(1 - impact\.baselineError, estimateDecimals\)/);
    expect(SOURCE).toMatch(/formatPct\(1 - impact\.residualError, estimateDecimals\)/);
  });

  it('and to the tier beside them, which renders its own copy of the number', () => {
    // renderQualityTier is shared with the Test Sets list and hardcoded .toFixed(1),
    // so without this the same row showed "92%" and "92.2% est." together.
    expect(SOURCE).toMatch(
      /renderQualityTier\(impact\.qualityTier, impact\.qualityTierReason, 1 - impact\.baselineError, estimateDecimals\)/,
    );
    expect(TIER).toMatch(/\(accuracy \* 100\)\.toFixed\(decimals\)/);
  });

  it('keeps the default precision for the list that did not ask to change', () => {
    // TestSetDetail's own callers pass no `decimals`, so the parameter must default.
    expect(TIER).toMatch(/decimals = 1,/);
  });

  it('names a tier the server actually emits', () => {
    // A typo here would silently pin every estimate to whole percent, including the
    // measured ones that have earned the extra digit.
    expect(CURVE).toMatch(/MEASURED = "measured"/);
  });
});
