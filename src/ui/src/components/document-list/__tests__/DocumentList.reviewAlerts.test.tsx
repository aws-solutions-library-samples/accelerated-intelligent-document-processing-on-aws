// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Renders the whole document list to pin what the Start Review and Release Review
 * items of the toolbar's Review menu do with the server's answer: a refusal is shown in an alert above
 * the table, and only a successful single claim opens the document. The contexts
 * and the API client are mocked; the table, the selection and the toolbar are real.
 */

import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { graphqlMock, navigateMock, documentsRef, bulkSkipProps } = vi.hoisted(() => ({
  graphqlMock: vi.fn(),
  navigateMock: vi.fn(),
  documentsRef: { current: [] as Record<string, unknown>[] },
  bulkSkipProps: { current: null as null | Record<string, unknown> },
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('../../../api/auth-session', () => ({
  fetchSharedAuthSession: () => Promise.resolve({ tokens: { idToken: { payload: { 'cognito:username': 'admin' } } } }),
}));
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: null }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: true, isReviewerOnly: false, canWrite: true, canReview: true, canWorkReviewQueues: false }),
}));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
// The Review menu's queue settings come from their own hook; not under test here.
vi.mock('../../review-bulk/use-review-action-settings', () => ({
  default: () => ({ defaultQueueEnabled: true, bulkSkipSyncLimit: 50, loading: false }),
}));
vi.mock('../CircuitBreakerBadge', () => ({ default: () => <span /> }));
// The bulk dialog is tested on its own; here only what the list hands it, and what
// the list does with its result.
vi.mock('../../review-bulk/BulkSkipModal', () => ({
  default: (props: Record<string, unknown>) => {
    bulkSkipProps.current = props;
    return <span data-testid="bulk-skip" />;
  },
}));
// Closed and unrelated to review; it reads the configuration-versions hook in full.
vi.mock('../../common/ReprocessDocumentModal', () => ({ default: () => null }));
vi.mock('../../../contexts/documents', () => ({
  default: () => ({
    documents: documentsRef.current,
    isDocumentsListLoading: false,
    hasListBeenLoaded: true,
    setIsDocumentsListLoading: vi.fn(),
    setPeriodsToLoad: vi.fn(),
    setSelectedItems: vi.fn(),
    setToolsOpen: vi.fn(),
    periodsToLoad: 1,
    customDateRange: null,
    setCustomDateRange: vi.fn(),
    documentView: 'PRODUCTION',
    setDocumentView: vi.fn(),
    latestTruncated: false,
    getDocumentDetailsFromIds: vi.fn(),
    deleteDocuments: vi.fn(),
    reprocessDocuments: vi.fn(),
    abortWorkflows: vi.fn(),
  }),
}));

import DocumentList from '../DocumentList';

const doc = (ObjectKey: string, over: Record<string, unknown> = {}) => ({
  ObjectKey,
  ObjectStatus: 'COMPLETED',
  InitialEventTime: '2026-10-01T00:00:00Z',
  HITLTriggered: true,
  HITLStatus: 'Review Pending',
  ...over,
});

const refused = (message: string) => Promise.reject({ errors: [{ message }] });

// Start Review and Release Review live in the toolbar's Review menu.
const reviewMenu = () =>
  createWrapper()
    .findAllButtonDropdowns()
    .find((d) => (d.findNativeButton().getElement().getAttribute('aria-label') ?? '') === 'Review')!;
const ITEM_IDS: Record<string, string> = { 'Start Review': 'claim', 'Release Review': 'release', 'Skip All Reviews': 'skip-selected' };
const chooseReview = (label: string) => {
  const menu = reviewMenu();
  menu.openDropdown();
  menu.findItemById(ITEM_IDS[label])!.click();
};
const reviewItemDisabled = (label: string): boolean => {
  const menu = reviewMenu();
  menu.openDropdown();
  const item = menu.findItemById(ITEM_IDS[label])!.getElement();
  const disabled = item.getAttribute('aria-disabled') === 'true' || !!item.querySelector('[aria-disabled="true"]');
  menu.openDropdown();
  return disabled;
};

const selectRow = (index: number) => {
  const area = createWrapper().findTable()!.findRowSelectionArea(index);
  fireEvent.click(area!.find('input')!.getElement());
};

// The closed Abort modal renders an Alert of its own, so look only for the review refusal.
const alertText = () =>
  createWrapper()
    .findAllAlerts()
    .map((alert) => alert.getElement().textContent ?? '')
    .find((text) => text.includes('was refused')) ?? null;

beforeEach(() => {
  graphqlMock.mockReset();
  navigateMock.mockReset();
  localStorage.clear();
});

describe('DocumentList review alerts', () => {
  it('shows the refusal for Start Review and does not open the document', async () => {
    documentsRef.current = [doc('owed.pdf')];
    graphqlMock.mockImplementation(() => refused('NO_REVIEW_PENDING: this document has no review waiting, so there is nothing to start'));
    render(<DocumentList />);
    selectRow(1);
    chooseReview('Start Review');

    await waitFor(() => expect(alertText()).toContain('Start Review was refused'));
    expect(alertText()).toContain('owed.pdf: NO_REVIEW_PENDING');
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it('opens the document when the one claim succeeds, with no alert', async () => {
    documentsRef.current = [doc('owed.pdf')];
    graphqlMock.mockResolvedValue({
      data: { claimReview: { HITLReviewOwner: 'admin', HITLReviewOwnerEmail: 'a@example.com', HITLStatus: 'InProgress' } },
    });
    render(<DocumentList />);
    selectRow(1);
    chooseReview('Start Review');

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/documents/owed.pdf'));
    expect(alertText()).toBeNull();
  });

  it('sends Release Review only for the claimed rows of a mixed selection and shows a refusal', async () => {
    documentsRef.current = [
      doc('claimed.pdf', { HITLReviewOwner: 'rev', HITLStatus: 'InProgress' }),
      doc('unclaimed.pdf'),
      doc('finished.pdf', { HITLReviewOwner: 'rev', HITLStatus: 'Completed' }),
    ];
    graphqlMock.mockImplementation(() => refused('REVIEW_FINISHED: this review is already finished, so there is nothing to release'));
    render(<DocumentList />);
    [1, 2, 3].forEach(selectRow);
    chooseReview('Release Review');

    await waitFor(() => expect(alertText()).toContain('Release Review was refused'));
    expect(alertText()).toContain('claimed.pdf: REVIEW_FINISHED');
    expect(graphqlMock.mock.calls.map(([args]) => args.variables.objectKey)).toEqual(['claimed.pdf']);
  });

  it('shows no alert when every release succeeds', async () => {
    documentsRef.current = [doc('claimed.pdf', { HITLReviewOwner: 'rev', HITLStatus: 'InProgress' })];
    graphqlMock.mockResolvedValue({ data: { releaseReview: { HITLStatus: 'Review Pending' } } });
    render(<DocumentList />);
    selectRow(1);
    chooseReview('Release Review');

    await waitFor(() => expect(graphqlMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(graphqlMock).toHaveBeenCalledTimes(1));
    expect(alertText()).toBeNull();

    // Clearing the selection disables the button too, so select the row again: it
    // stays disabled only because the list now shows the row as unclaimed.
    selectRow(1);
    expect(reviewItemDisabled('Release Review')).toBe(true);
    expect(reviewItemDisabled('Start Review')).toBe(false);
  });

  it('clears a Start Review refusal when a later Release Review succeeds', async () => {
    documentsRef.current = [doc('owed.pdf'), doc('claimed.pdf', { HITLReviewOwner: 'rev', HITLStatus: 'InProgress' })];
    graphqlMock.mockImplementation(({ query }: { query: string }) =>
      query.includes('releaseReview')
        ? Promise.resolve({ data: { releaseReview: { HITLStatus: 'Review Pending' } } })
        : refused('NO_REVIEW_PENDING: this document has no review waiting, so there is nothing to start'),
    );
    render(<DocumentList />);
    selectRow(1);
    chooseReview('Start Review');
    await waitFor(() => expect(alertText()).toContain('Start Review was refused'));

    selectRow(2);
    chooseReview('Release Review');
    await waitFor(() => expect(graphqlMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(alertText()).toBeNull());
  });

  it('opens bulk Skip All Reviews with only the skippable rows, and patches them in place', async () => {
    documentsRef.current = [doc('owed.pdf'), doc('done.pdf', { HITLStatus: 'Review Skipped', HITLCompleted: true })];
    render(<DocumentList />);
    [1, 2].forEach(selectRow);
    const before = createWrapper()
      .findTable()!
      .findRows()
      .map((r) => r.getElement().textContent ?? '')
      .find((t) => t.includes('owed.pdf'));
    expect(before).not.toContain('Review Skipped');
    chooseReview('Skip All Reviews');
    await waitFor(() => expect(bulkSkipProps.current).not.toBeNull());
    expect(bulkSkipProps.current!.objectKeys).toEqual(['owed.pdf']);
    const onFinished = bulkSkipProps.current!.onFinished as (r: unknown) => void;
    await waitFor(() =>
      onFinished({
        jobId: 'brj-1',
        counts: [{ outcome: 'SKIPPED', count: 1 }],
        results: [{ objectKey: 'owed.pdf', outcome: 'SKIPPED', kind: 'legacy', detail: null }],
      }),
    );
    const owedRow = () =>
      createWrapper()
        .findTable()!
        .findRows()
        .map((r) => r.getElement().textContent ?? '')
        .find((t) => t.includes('owed.pdf')) ?? '';
    await waitFor(() => expect(owedRow()).toContain('Review Skipped'));
  });
});
