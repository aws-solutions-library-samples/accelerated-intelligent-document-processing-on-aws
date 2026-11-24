// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The configuration page used to hide the Policy Schema tab, and switch on the BDA
 * actions, for a deployment whose pattern setting named the first of the three separate
 * patterns. Those patterns are gone (processing is one pattern, in BDA mode or Pipeline
 * mode), so the check could never be true and was removed. This guards the removal: the
 * page's source names that pattern nowhere.
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const source = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../ConfigurationLayout.tsx'), 'utf8');

describe('the configuration page', () => {
  it('has no check for the first of the removed patterns', () => {
    expect(source).not.toContain('isPattern1');
    expect(source).not.toContain('Pattern1');
  });

  it('always shows the Policy Schema tab and the Routing tab', () => {
    expect(source).toMatch(/\n\s+showRuleSchema\n/);
    expect(source).toMatch(/\n\s+showRoutingTab\n/);
    expect(source).not.toContain('showRuleSchema={');
  });

  it('decides the BDA actions from the configuration alone', () => {
    expect(source).toContain('const isBdaConfigActive = Boolean(mergedConfig?.use_bda || formValues?.use_bda);');
  });

  it('reports a Policy Schema problem under that name', () => {
    expect(source).toContain('Policy Schema: ');
    expect(source).not.toContain('Rule Schema: ');
  });
});
