// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What the save confirmation names.
 *
 * It used to take the last segment of the baseline key, which is always `result.json` —
 * so every save on every document in every set announced "result.json is now marked
 * reviewed". Reported alongside the progress-counter bug it sat next to, and the pair
 * is what made the screen self-contradictory: a toast claiming a review had been
 * recorded, above a bar reading 0 of 73.
 */

import { describe, expect, it } from 'vitest';

import { documentNameFromBaselineKey } from '../AnnotationWorkspace';

describe('documentNameFromBaselineKey', () => {
  it('names the document, not the result file every document shares', () => {
    expect(documentNameFromBaselineKey('ts1/baseline/XT_00100.PDF/sections/1/result.json')).toBe('XT_00100.PDF');
  });

  it('is unaffected by which section was saved', () => {
    // Section 12 of a 16-section packet still identifies the same document.
    expect(documentNameFromBaselineKey('ts1/baseline/packet_0001.pdf/sections/12/result.json')).toBe('packet_0001.pdf');
  });

  it('handles a set id that itself contains the word baseline', () => {
    // Splitting on '/baseline/' takes the first occurrence, which is the delimiter.
    expect(documentNameFromBaselineKey('my-baseline-set/baseline/doc.pdf/sections/1/result.json')).toBe('doc.pdf');
  });

  it('falls back to the whole key rather than showing nothing', () => {
    // An unexpected shape should still say something identifiable, not an empty string
    // in the middle of a sentence.
    expect(documentNameFromBaselineKey('some/other/key.json')).toBe('some/other/key.json');
    expect(documentNameFromBaselineKey('')).toBe('');
  });
});
