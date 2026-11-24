// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The statuses the UI offers Abort for are the ones the abort resolver accepts: a
 * status the resolver accepts and the UI leaves out is a document that cannot be
 * aborted from the web UI (the three policy rule validation statuses were), and one
 * the UI has and the resolver refuses is a button that fails.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { ABORTABLE_STATUSES, SLOW_POLL_STATUSES, TERMINAL_STATUSES } from '../document-statuses';

const RESOLVER = join(
  __dirname,
  '..',
  '..',
  '..',
  '..',
  '..',
  '..',
  'nested',
  'api-resolvers',
  'src',
  'lambda',
  'abort_workflow_resolver',
  'index.py',
);

/** The members of the resolver's ABORTABLE_STATUSES set, read from its source. */
const resolverStatuses = (): string[] => {
  const source = readFileSync(RESOLVER, 'utf-8');
  const block = /^ABORTABLE_STATUSES = \{([\s\S]*?)^\}/m.exec(source);
  if (!block) throw new Error('ABORTABLE_STATUSES not found in the abort resolver');
  return [...block[1].matchAll(/Status\.([A-Z_]+)/g)].map((match) => match[1]);
};

describe('the statuses a document can be aborted in', () => {
  it('are the ones the abort resolver accepts', () => {
    const accepted = resolverStatuses();

    expect(accepted.length).toBeGreaterThan(0);
    expect([...ABORTABLE_STATUSES].sort()).toEqual([...accepted].sort());
  });

  it.each(['RULE_VALIDATION_POLICY_CLASSIFICATION', 'RULE_VALIDATION', 'RULE_VALIDATION_ORCHESTRATOR'])(
    'include %s, a status of policy rule validation',
    (status) => {
      expect(ABORTABLE_STATUSES).toContain(status);
    },
  );

  it('are never ones after which a document no longer changes', () => {
    expect(ABORTABLE_STATUSES.filter((status) => TERMINAL_STATUSES.includes(status))).toEqual([]);
    expect(SLOW_POLL_STATUSES.every((status) => ABORTABLE_STATUSES.includes(status))).toBe(true);
  });
});
