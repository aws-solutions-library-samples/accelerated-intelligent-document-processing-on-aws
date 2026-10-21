// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** My Work › Queues rows: production and Test Studio queues link to their queue pages. */

import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));

import type { ReviewQueueSummary, TestSetQueueSummary } from '../../../graphql/generated/operation-types';
import { productionQueuePath, testSetQueuePath } from '../../../routes/constants';
import { fromProductionQueue, fromTestSetQueue } from '../MyQueuesPage';

describe('My Work › Queues rows', () => {
  it('maps a production queue to its page, with pending and the caller’s items', () => {
    const row = fromProductionQueue({
      name: 'claims',
      displayName: 'Claims',
      depth: 7,
      unclaimed: 4,
      claimed: 3,
      claimedByMe: 2,
      priority: 5,
      oldestEnqueuedAt: '2026-10-01T00:00:00Z',
    } as ReviewQueueSummary);
    expect(row).toMatchObject({
      id: 'p:claims',
      source: 'production',
      name: 'Claims',
      href: `#${productionQueuePath('claims')}`,
      items: 7,
      pending: 4,
      mine: 2,
    });
  });

  it('maps a Test Studio queue to its page, counting the caller’s documents from Tasks', () => {
    const row = fromTestSetQueue({ id: 'ts1', name: 'Invoices', fileCount: 10, pendingDocs: 6 } as TestSetQueueSummary, 1);
    expect(row).toMatchObject({
      id: 't:ts1',
      source: 'test-set',
      name: 'Invoices',
      href: `#${testSetQueuePath('ts1')}`,
      items: 10,
      pending: 6,
      mine: 1,
    });
  });
});
