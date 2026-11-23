// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * "From files in S3" over the Input bucket adds documents without ground truth as
 * unlabeled (#1817), so the note the wizard shows for that bucket must say so. It
 * used to say they were skipped, which contradicted the Configure step's note that
 * they arrive unlabeled.
 *
 * Asserted at source level, as the sibling CreateTestSetWizard tests are: rendering
 * the wizard needs the GraphQL client, settings, role hooks and a file input.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const WIZARD = readFileSync(join(__dirname, '..', 'CreateTestSetWizard.tsx'), 'utf-8');

/** The text of the Alert rendered when the Input bucket is selected. */
const inputBucketNote = (): string => {
  const match = WIZARD.match(/bucket\.value === 'input' && \(\s*<Alert type="info">([\s\S]*?)<\/Alert>/);
  expect(match, 'could not find the Input-bucket note').not.toBeNull();
  return (match as RegExpMatchArray)[1].replace(/\s+/g, ' ').trim();
};

describe('CreateTestSetWizard Input-bucket note', () => {
  it('says documents without ground truth are added unlabeled', () => {
    expect(inputBucketNote()).toMatch(/without ground truth in the evaluation baseline bucket are added unlabeled/);
  });

  it('no longer says they are skipped', () => {
    expect(inputBucketNote()).not.toMatch(/skipped/i);
    expect(WIZARD).not.toMatch(/skipped rather than failing/);
  });
});
