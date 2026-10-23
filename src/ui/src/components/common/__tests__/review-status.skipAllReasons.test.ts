// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Skip All Reviews offers exactly the reasons the server accepts.
 *
 * The resolver refuses any reason outside `SKIP_ALL_REASONS` in statuses.py, so a
 * reason added on one side only is either never offered or always refused.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { SKIP_ALL_REASONS } from '../review-status';

const STATUSES_PY = join(__dirname, '../../../../../../lib/idp_common_pkg/idp_common/review_queues/statuses.py');

describe('SKIP_ALL_REASONS', () => {
  it('matches the server list, in order', () => {
    const source = readFileSync(STATUSES_PY, 'utf-8');
    const block = /SKIP_ALL_REASONS = \(([\s\S]*?)\n\)/.exec(source);
    expect(block, 'SKIP_ALL_REASONS not found in statuses.py').not.toBeNull();
    const server = [...block![1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect(server.length).toBeGreaterThan(0);
    expect([...SKIP_ALL_REASONS]).toEqual(server);
  });
});
