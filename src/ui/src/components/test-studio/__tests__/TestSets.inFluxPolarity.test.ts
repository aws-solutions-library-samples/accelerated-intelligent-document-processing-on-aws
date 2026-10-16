// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which statuses this table treats as "still being written", and which way round
 * it asks.
 *
 * Found on a live stack: 22 of 31 rows were permanently disabled — no checkbox,
 * so no Actions menu, so no way to delete them — and no link to their detail
 * pages. Those rows are written by the benchmark harness straight to the tracking
 * table, bypassing the test-set API, and said `status: READY`. Every gate in
 * TestSets.tsx asked `status !== 'COMPLETED' && status !== 'FAILED'`, which reads
 * an unrecognised status as in-flux, so a set that was in fact finished was
 * rendered inert. The same comparison drove the 3s poll, which therefore never
 * stopped.
 *
 * The harness now writes a status the API actually produces, which fixes the
 * instance. These tests pin the class: the question is asked as membership of a
 * closed in-flux set, so the next out-of-band writer — or a status added to the
 * resolver before this file hears about it — degrades to a usable row.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { IN_FLUX_TEST_SET_STATUSES, isInFlux } from '../TestSets';

const SOURCE = readFileSync(join(__dirname, '..', 'TestSets.tsx'), 'utf-8');

const REPO = join(__dirname, '..', '..', '..', '..', '..', '..');
const RESOLVER = readFileSync(join(REPO, 'nested', 'api-resolvers', 'src', 'lambda', 'test_set_resolver', 'index.py'), 'utf-8');

describe('the in-flux predicate', () => {
  it('treats an unrecognised status as settled, not as in-flux', () => {
    // The whole defect, in one assertion. `READY` is the value that caused it;
    // the other two stand for "some status nobody here has heard of".
    expect(isInFlux('READY')).toBe(false);
    expect(isInFlux('ARCHIVED')).toBe(false);
    expect(isInFlux('')).toBe(false);
    expect(isInFlux(undefined)).toBe(false);
    expect(isInFlux(null)).toBe(false);
  });

  it("treats the resolver's in-flux statuses as in-flux", () => {
    for (const status of ['QUEUED', 'COPYING', 'GENERATING', 'UPDATING']) {
      expect(isInFlux(status), status).toBe(true);
    }
  });

  it('treats the terminal statuses as settled', () => {
    expect(isInFlux('COMPLETED')).toBe(false);
    expect(isInFlux('FAILED')).toBe(false);
  });

  it("matches the resolver's own list rather than restating it", () => {
    // Read out of the backend, because a copied rule is a rule that drifts: a
    // status added there and not here would be rendered as a finished set.
    const match = RESOLVER.match(/IN_FLUX_TEST_SET_STATUSES = frozenset\(\{([^}]*)\}\)/s);
    expect(match, 'IN_FLUX_TEST_SET_STATUSES not found in the resolver').not.toBeNull();
    const backend = [...(match as RegExpMatchArray)[1].matchAll(/"([A-Z_]+)"/g)].map((m) => m[1]);
    expect(backend.length).toBeGreaterThan(0);
    expect([...IN_FLUX_TEST_SET_STATUSES].sort()).toEqual(backend.sort());
  });
});

describe('the call sites', () => {
  it('ask membership, not inequality against the terminal statuses', () => {
    // The shape that caused this, in any spacing, anywhere in the file. Finding it
    // means a surface has gone back to a closed-world reading of `status`.
    expect(SOURCE).not.toMatch(/status\s*!==\s*'COMPLETED'\s*&&[^;]*!==\s*'FAILED'/);
  });

  it('gates row selection, the detail link and the poll on it', () => {
    // Named individually: these are the three the defect was visible through, and
    // a regression in any one of them is invisible in the other two.
    expect(SOURCE).toMatch(/isItemDisabled=\{\(item\) => isInFlux\(item\.status\)\}/);
    expect(SOURCE).toMatch(/isInFlux\(item\.status\) \? item\.name :/);
    expect(SOURCE).toMatch(/hasActiveTestSets = testSets\.some\(\(testSet\) => isInFlux\(testSet\.status\)\)/);
  });

  it('keeps the append action on exactly COMPLETED, matching the resolver guard', () => {
    // Deliberately NOT isInFlux: _begin_test_set_append rejects anything else, so
    // widening this would move the refusal to a server-side error. Asserted so the
    // exception stays a decision rather than reading as a missed call site.
    expect(SOURCE).toMatch(/selectedItems\[0\]\?\.status !== 'COMPLETED'/);
    expect(RESOLVER).toMatch(/def _begin_test_set_append[\s\S]{0,600}?status"\) != "COMPLETED"/);
  });
});
