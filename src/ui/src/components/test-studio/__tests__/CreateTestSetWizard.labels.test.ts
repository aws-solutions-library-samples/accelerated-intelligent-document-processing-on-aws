// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The wizard's submit button does not share a label with the button that opens it.
 *
 * Found while driving the wizard: two buttons reading "Create test set" were on screen
 * at the same time — the Test Sets page header and the wizard footer — and clicking the
 * wrong one reopened the wizard mid-flow. Position tells them apart for a sighted user
 * and nothing does for a screen reader, which announces both identically while one opens
 * a form and the other commits it.
 *
 * Pinned because the obvious "improvement" is to make the footer more descriptive by
 * restoring the longer label, which is what created the collision.
 *
 * Asserted at source level, as the sibling AnnotationWorkspace tests are: rendering the
 * wizard needs the GraphQL client, settings, role hooks and a file input, for what is one
 * i18n string.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const WIZARD = readFileSync(join(HERE, 'CreateTestSetWizard.tsx'), 'utf-8');
const TEST_SETS = readFileSync(join(HERE, 'TestSets.tsx'), 'utf-8');

/** The wizard's submitButton i18n string. */
const submitLabel = (): string => {
  const match = WIZARD.match(/submitButton:[^,\n]*'([^']+)',?\s*$/m);
  expect(match, 'could not find the wizard submitButton label').not.toBeNull();
  return (match as RegExpMatchArray)[1];
};

describe('CreateTestSetWizard button labels', () => {
  it('does not reuse the label of the button that opens it', () => {
    // The page keeps the descriptive label — it is the one you look for on a full page
    // of test sets.
    expect(TEST_SETS).toMatch(/Create test set/);
    // The footer does not, because by then the dialog heading has supplied the context.
    expect(submitLabel()).toBe('Create');
    expect(submitLabel()).not.toBe('Create test set');
  });

  it('keeps the synthetic-generation label distinct too', () => {
    // The same footer submits a different operation when generating, and "Create" would
    // understate it — it starts a background job that costs money.
    expect(WIZARD).toMatch(/isGenerate \? 'Generate documents' : 'Create'/);
  });
});
