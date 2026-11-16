// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** The UI's outcome list is the server's (`BulkOutcome` in bulk_models.py), in order. */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { BULK_OUTCOMES } from '../bulk-outcomes';

const MODELS = resolve(__dirname, '../../../../../../lib/idp_common_pkg/idp_common/review_queues/bulk_models.py');

describe('bulk outcomes', () => {
  it('match the server enum in the same order', () => {
    const source = readFileSync(MODELS, 'utf-8');
    const block = /class BulkOutcome\(str, Enum\):([\s\S]*?)\n\n\n/.exec(source)?.[1] ?? '';
    const values = [...block.matchAll(/^\s+[A-Z_]+ = "([A-Z_]+)"/gm)].map((m) => m[1]);
    expect(values.length).toBeGreaterThan(10);
    expect(Object.keys(BULK_OUTCOMES)).toEqual(values);
  });
});
