// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Home › Queues by role. A Reviewer works Production review queues only, and the server
 * refuses them `listTestSetQueues`, so the page must not ask: asking would put a
 * "Showing the last loaded queues" warning on every poll. An Annotator lists both kinds.
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { role, fetchMyReviewQueues, listTestSetQueues } = vi.hoisted(() => ({
  role: { canWorkTestSetQueues: false, loading: false },
  fetchMyReviewQueues: vi.fn(),
  listTestSetQueues: vi.fn(),
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../review-api', () => ({ fetchMyReviewQueues: (...a: unknown[]) => fetchMyReviewQueues(...a) }));
vi.mock('../reviewer-assignment', () => ({ listTestSetQueues: (...a: unknown[]) => listTestSetQueues(...a) }));

import MyQueuesPage from '../MyQueuesPage';

const PRODUCTION = { name: 'claims', displayName: 'Claims', depth: 2, unclaimed: 1, claimed: 1, claimedByMe: 0 };
const TEST_SET = { id: 'ts1', name: 'Invoices', fileCount: 3, pendingDocs: 3 };

beforeEach(() => {
  document.body.innerHTML = '';
  fetchMyReviewQueues.mockReset().mockResolvedValue({ queues: [PRODUCTION] });
  listTestSetQueues.mockReset().mockResolvedValue([TEST_SET]);
});

describe('Home › Queues', () => {
  it('makes no listTestSetQueues call for a Reviewer-only user and shows no error', async () => {
    Object.assign(role, { canWorkTestSetQueues: false, loading: false });
    render(<MyQueuesPage />);

    await waitFor(() => expect(screen.getByText('Claims')).toBeTruthy());
    expect(fetchMyReviewQueues).toHaveBeenCalled();
    expect(listTestSetQueues).not.toHaveBeenCalled();
    expect(createWrapper(document.body).findAlert()).toBeNull();
    expect(screen.queryByText('Invoices')).toBeNull();
  });

  it('lists production and Test Studio queues for an Annotator', async () => {
    Object.assign(role, { canWorkTestSetQueues: true, loading: false });
    render(<MyQueuesPage />);

    await waitFor(() => expect(screen.getByText('Invoices')).toBeTruthy());
    expect(screen.getByText('Claims')).toBeTruthy();
    expect(fetchMyReviewQueues).toHaveBeenCalled();
    expect(listTestSetQueues).toHaveBeenCalled();
    expect(createWrapper(document.body).findAlert()).toBeNull();
  });

  it('waits for the role before loading', () => {
    Object.assign(role, { canWorkTestSetQueues: false, loading: true });
    render(<MyQueuesPage />);

    expect(fetchMyReviewQueues).not.toHaveBeenCalled();
    expect(listTestSetQueues).not.toHaveBeenCalled();
  });
});
