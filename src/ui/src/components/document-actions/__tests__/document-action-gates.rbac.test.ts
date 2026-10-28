// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The document page offers each action to exactly the groups the server admits for it.
 * What has to hold: for each action, the groups allowed are those every one of its
 * operations admits in scripts/api_rbac_expectations.yaml (ANY_GROUP and ANY meaning every
 * group), less the groups the server admits only for some objects (`object_scoped_roles`);
 * ACTION_GROUPS says the same; and a user in exactly one group is offered an action when,
 * and only when, that group is allowed. Edit is navigation, so it has its own table.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { load } from 'js-yaml';
import { describe, expect, it } from 'vitest';

import { ACTION_GROUPS, ACTION_OPERATIONS, ALL_GROUPS, documentActionGates } from '../document-action-gates';
import type { ActionRoles, AppGroup, DocumentActionId, GateInput } from '../document-action-gates';

interface Expectation {
  groups: string[] | string;
  object_scoped_roles?: string[];
}

const expectations = (
  load(readFileSync(join(__dirname, '..', '..', '..', '..', '..', '..', 'scripts', 'api_rbac_expectations.yaml'), 'utf-8')) as {
    operations: Record<string, Expectation>;
  }
).operations;

const admitted = (operation: string): AppGroup[] => {
  const entry = expectations[operation];
  if (!entry) throw new Error(`${operation} is not in api_rbac_expectations.yaml`);
  const groups = entry.groups === 'ANY_GROUP' || entry.groups === 'ANY' ? [...ALL_GROUPS] : (entry.groups as AppGroup[]);
  const scoped = entry.object_scoped_roles ?? [];
  return groups.filter((g) => !scoped.includes(g));
};

const allowedFor = (operations: string[]): AppGroup[] => ALL_GROUPS.filter((g) => operations.every((op) => admitted(op).includes(g)));

const rolesOf = (group: AppGroup): ActionRoles => ({
  isAdmin: group === 'Admin',
  isAuthor: group === 'Author',
  isReviewer: group === 'Reviewer',
  isAnnotator: group === 'Annotator',
  isViewer: group === 'Viewer',
  isReviewerOnly: group === 'Reviewer',
  canWorkReviewQueues: group === 'Admin' || group === 'Annotator',
});

/** A document where every action applies: a legacy review pending and unclaimed, abortable, with every report. */
const input = (roles: ActionRoles, over: Partial<GateInput['doc']> = {}): GateInput => {
  const doc = {
    objectKey: 'in/a.pdf',
    objectStatus: 'COMPLETED',
    hitlStatus: 'PendingReview',
    hitlTriggered: true,
    evaluationStatus: 'COMPLETED',
    summaryReportUri: 's3://o/summary.md',
    evaluationReportUri: 's3://o/eval.md',
    ruleValidationResultUri: 's3://o/rules.json',
    executionArn: 'arn:aws:states:us-east-1:000000000000:execution:sm:x',
    sectionCount: 2,
    pageCount: 3,
    pagesWithText: 3,
    ...over,
  };
  return { roles, doc, displayed: doc, isHistorical: false, bdaMode: false, configLoading: false, baselineRequestInFlight: false };
};

const ACTIONS = Object.keys(ACTION_OPERATIONS) as Exclude<DocumentActionId, 'edit'>[];

describe('the document page actions mirror the server RBAC', () => {
  it.each(ACTIONS)('%s is offered to the groups its operations admit', (action) => {
    expect([...ACTION_GROUPS[action]].sort()).toEqual(allowedFor(ACTION_OPERATIONS[action].operations).sort());
  });

  it('Edit Page Text is the intersection of uploadDocument and processChanges, which is Admin only', () => {
    expect(allowedFor(ACTION_OPERATIONS['page-text'].operations)).toEqual(['Admin']);
  });

  describe.each(ALL_GROUPS)('a user in the %s group', (group) => {
    const gates = documentActionGates(input(rolesOf(group)));
    it.each(ACTIONS)('is offered %s exactly when the server admits the group', (action) => {
      expect(gates[action].shown).toBe(allowedFor(ACTION_OPERATIONS[action].operations).includes(group));
    });
  });
});

describe('Edit on the document page', () => {
  it.each([
    ['Admin', true],
    ['Author', true],
    ['Reviewer', true],
    ['Viewer', false],
    ['Annotator', false],
  ] as const)('outside a review queue, a user in the %s group is offered it: %s', (group, shown) => {
    const gates = documentActionGates(input(rolesOf(group), { hitlStatus: null, hitlTriggered: false }));
    expect(gates.edit.shown).toBe(shown);
    expect(gates.editInQueue).toBe(false);
  });

  it.each(['Admin', 'Annotator'] as const)('in a review queue, a %s (who works queues) is offered it, into the queue', (group) => {
    const gates = documentActionGates(input(rolesOf(group), { hitlStatus: 'in_review' }));
    expect(gates.edit.shown).toBe(true);
    expect(gates.editInQueue).toBe(true);
  });

  it('in a review queue, a Viewer is not offered it', () => {
    const gates = documentActionGates(input(rolesOf('Viewer'), { hitlStatus: 'in_review' }));
    expect(gates.edit.shown).toBe(false);
  });
});
