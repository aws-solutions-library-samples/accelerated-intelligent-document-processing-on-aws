// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit page holds no mode-specific code: what applies in a mode, and for which
 * role, is the mode's model's business (direct-edit-model, production-item-edit-model,
 * test-set-item-edit-model). A role flag, a test-set or queue-item identifier, or a
 * comparison on the mode in the page or its board would put a mode back into the shared
 * plumbing, which is what the model exists to prevent.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const MODE_SPECIFIC = /testSetId|reviewObjectKey|QueueItemState|isAdmin|isAnnotator|isAuthor|canWrite|detail\.item|mode ===/;

describe('the edit page', () => {
  it.each(['EditPage.tsx', 'use-page-class-board.tsx'])('%s has no mode or role branching', (file) => {
    const source = readFileSync(join(HERE, file), 'utf-8');
    expect(source.length).toBeGreaterThan(0);
    expect(source).not.toMatch(MODE_SPECIFIC);
  });
});
