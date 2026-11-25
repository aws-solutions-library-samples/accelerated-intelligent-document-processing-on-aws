// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A production review item as a work row: it keeps the queue's id (links and moves use
 * it) and carries the queue's name when the API reports one; the table shows the name,
 * marked when the item was escalated. It carries the routing rule that placed it, the
 * reason the routing rule gave and how many policy rules the document failed; a Test
 * Studio row has none of them. A row holds data: `workItemView` words it for the table.
 */

import { createIntl } from 'react-intl';
import { describe, expect, it } from 'vitest';

import type { ReviewItem, TestSetTask } from '../../../graphql/generated/operation-types';
import { fromReviewItem, fromTestSetTask, workItemView } from '../work-items';

const intl = createIntl({ locale: 'en' });

const item = (over: Partial<ReviewItem> = {}): ReviewItem =>
  ({
    objectKey: 'in/doc.pdf',
    cycleId: 'c1',
    profile: 'default',
    queue: 'claims',
    priority: 50,
    state: 'OPEN',
    enqueuedAt: '2026-10-22T00:00:00.000Z',
    reasons: [],
    reasonCount: 0,
    isMine: false,
    ...over,
  }) as ReviewItem;

describe('fromReviewItem', () => {
  it('keeps the queue id and adds the queue name', () => {
    const row = fromReviewItem('review')(item({ queueName: 'High-value claims' }));

    expect(row.queue).toBe('claims');
    expect(row.queueName).toBe('High-value claims');
  });

  it.each([[undefined], [null], ['']])('has no name when the API reports %j', (queueName) => {
    const row = fromReviewItem('review')(item({ queueName }));

    expect(row.queue).toBe('claims');
    expect(row.queueName).toBeNull();
  });

  it('flags an escalated item, and the table marks the queue it shows: the name, or the id when there is none', () => {
    const row = fromReviewItem('review')(item({ queueName: 'High-value claims', escalated: true }));

    // The id and the name stay as stored: links, moves and the filter use them.
    expect(row).toMatchObject({ queue: 'claims', queueName: 'High-value claims', escalated: true });
    expect(workItemView(row, intl).queue).toBe('High-value claims (escalated)');
    expect(workItemView(fromReviewItem('review')(item({ escalated: true })), intl).queue).toBe('claims (escalated)');
  });

  it('shows the name of the queue, or its id when there is none, for an item that is not escalated', () => {
    expect(workItemView(fromReviewItem('review')(item({ queueName: 'High-value claims' })), intl).queue).toBe('High-value claims');
    expect(workItemView(fromReviewItem('review')(item()), intl).queue).toBe('claims');
  });
});

describe('fromReviewItem: the routing rule', () => {
  it('carries the rule that placed the item, for the reason column', () => {
    const row = fromReviewItem('review')(
      item({
        routedBy: {
          tableId: 'review-routing',
          ruleId: 'claims',
          ruleLabel: 'Claims',
          placedQueue: 'claims',
          fallbackReason: '',
          inputs: '{}',
        },
      }),
    );

    expect(row.routedBy).toEqual({ ruleId: 'claims', ruleLabel: 'Claims', fallbackReason: '' });
    expect(row.reason).toBeNull();
  });

  it('has none for an item no rule placed', () => {
    expect(fromReviewItem('review')(item({ routedBy: null })).routedBy).toBeNull();
    expect(fromReviewItem('review')(item()).routedBy).toBeNull();
  });
});

describe('fromReviewItem: the routing reason and the failed policy rules', () => {
  const routedBy = (over: Partial<NonNullable<ReviewItem['routedBy']>> = {}): ReviewItem['routedBy'] => ({
    tableId: 'review-routing',
    ruleId: 'claims',
    ruleLabel: 'Claims',
    placedQueue: 'claims',
    fallbackReason: '',
    inputs: '{}',
    ...over,
  });
  const failure = (ruleId: string) => ({ ruleId, policy: 'prior_auth', name: ruleId, reason: 'Not met.', pages: [1] });

  it('carries the reason of the routing rule beside the first alert', () => {
    const row = fromReviewItem('review')(
      item({
        routedBy: routedBy({ reason: '  Invoice total 1250.00 is over 1000.  ', reasonSource: 'admin' }),
        reasons: [{ kind: 'low_confidence', sectionId: '1', field: 'total', label: 'Total', score: 0.41, threshold: 0.8 }],
        reasonCount: 3,
      }),
    );

    expect(row.routingReason).toBe('Invoice total 1250.00 is over 1000.');
    // The first alert is data; the table words it.
    expect(row.reason).toMatchObject({ kind: 'review', reason: { kind: 'low_confidence', label: 'Total' } });
    expect(workItemView(row, intl).reason).toBe('Low confidence: Total 0.41 < 0.80');
    expect(row.extraReasons).toBe(2);
  });

  it.each([[undefined], [null], [''], ['   ']])('has no routing reason when the API reports %j', (reason) => {
    expect(fromReviewItem('review')(item({ routedBy: routedBy({ reason }) })).routingReason).toBeNull();
  });

  it('counts the policy rules the document failed, which can be more than are listed', () => {
    const counted = fromReviewItem('review')(
      item({ routedBy: routedBy({ policyFailures: [failure('prior_auth.coverage')], policyFailureCount: 12 }) }),
    );
    expect(counted.policyFailureCount).toBe(12);

    // A count that is missing or behind the list never reads lower than the list.
    const listed = fromReviewItem('review')(
      item({ routedBy: routedBy({ policyFailures: [failure('prior_auth.coverage'), failure('prior_auth.necessity')] }) }),
    );
    expect(listed.policyFailureCount).toBe(2);
  });

  it('has neither for an item no rule placed, or placed before routing rules had a reason', () => {
    expect(fromReviewItem('review')(item())).toMatchObject({ routingReason: null, policyFailureCount: 0 });
    expect(fromReviewItem('review')(item({ routedBy: routedBy() }))).toMatchObject({ routingReason: null, policyFailureCount: 0 });
  });
});

describe('fromTestSetTask', () => {
  it('has no routing reason and no failed policy rule', () => {
    const row = fromTestSetTask({ testSetId: 'set-1', objectKey: 'set-1/doc.pdf', alertCount: 2 } as TestSetTask);

    expect(row).toMatchObject({ routingReason: null, policyFailureCount: 0, reason: { kind: 'alerts', count: 2 } });
    expect(workItemView(row, intl).reason).toBe('2 confidence alerts');
  });
});
