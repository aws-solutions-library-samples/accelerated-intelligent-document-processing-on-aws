// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A production queue's page, for an Admin. What has to hold: Queue Progress is a bar of
 * the documents assigned or in progress, of all the queue's documents (a finished item
 * leaves its queue); Key Metrics shows exactly Pending, Assigned, In Progress,
 * Annotators (how many are listed) and Priority; the Annotators table lists each listed
 * Annotator, and anyone still holding documents, with the documents assigned to each;
 * the Documents table shows Document, Source, Queue, Priority and State, then the row
 * Actions menu; and the page reports the queue's name for the breadcrumb.
 */

import React from 'react';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => ({ isAdmin: true, isAnnotator: false, loading: false }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../reviewer-assignment', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../reviewer-assignment')>()),
  listReviewQueues: async () => [
    {
      id: 'default',
      name: 'Default',
      description: '',
      priority: 50,
      checklist: [],
      rejectReasons: [],
      skipReasons: [],
      timeLimitMinutes: 60,
      eligibleAnnotators: ['a@example.com', 'b@example.com'],
      isDefaultQueue: true,
      enabled: true,
    },
  ],
  listAnnotatorEmails: async () => [],
}));
vi.mock('../review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../review-api')>()),
  fetchQueueItems: async () => ({
    items: [
      {
        objectKey: 'in/doc.pdf',
        cycleId: 'c1',
        profile: 'default',
        queue: 'default',
        priority: 50,
        state: 'OPEN',
        enqueuedAt: '2026-10-22T00:00:00.000Z',
        reasons: [],
        reasonCount: 0,
        isMine: false,
      },
    ],
    nextToken: null,
  }),
  fetchQueuesOverview: async () => ({
    queues: [
      {
        summary: { name: 'default', displayName: 'Default', depth: 5, unclaimed: 2, claimed: 3, inProgress: 1, annotators: 2 },
        reviewers: [
          { email: 'a@example.com', claimed: 2 },
          { email: 'gone@example.com', claimed: 1 },
        ],
      },
    ],
  }),
}));

import ReviewQueuePage from '../ReviewQueuePage';
import { queueAnnotatorRows } from '../QueueAnnotatorsTable';

const renderPage = async (onName?: (name: string) => void) => {
  render(
    <MemoryRouter initialEntries={['/review-queues/queues/default']}>
      <Routes>
        <Route path="/review-queues/queues/:queue" element={<ReviewQueuePage onName={onName} />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(createWrapper(document.body).findAllTables()).toHaveLength(2));
  await waitFor(() => expect(itemsTable().findRows()).toHaveLength(1));
};

const tables = () => createWrapper(document.body).findAllTables();
const annotatorsTable = () => tables()[0];
const itemsTable = () => tables()[1];
const headers = (t: ReturnType<typeof annotatorsTable>) => t.findColumnHeaders().map((h) => h.getElement().textContent);

describe('a production queue page', () => {
  it('shows the work underway in Queue Progress, before Key Metrics', async () => {
    await renderPage();
    const bar = createWrapper(document.body).findProgressBar()!;
    // claimed 3 (assigned or in progress) of unclaimed 2 + claimed 3.
    expect(bar.findAdditionalInfo()!.getElement().textContent).toBe('3 of 5 documents assigned or in progress');
    const titles = createWrapper(document.body)
      .findAllContainers()
      .map((c) => c.findHeader()?.getElement().textContent);
    expect(titles.indexOf('Queue Progress')).toBeLessThan(titles.indexOf('Key Metrics'));
  });

  it('shows Pending, Assigned, In Progress, Annotators and Priority in Key Metrics', async () => {
    await renderPage();
    const pairs = createWrapper(document.body).findKeyValuePairs()!;
    expect(pairs.findItems().map((i) => i.findLabel()?.getElement().textContent)).toEqual([
      'Pending',
      'Assigned',
      'In Progress',
      'Annotators',
      'Priority',
    ]);
    expect(pairs.findItems().map((i) => i.findValue()?.getElement().textContent)).toEqual(['2', '2', '1', '2', '50']);
  });

  it('lists each Annotator with the documents assigned to them', async () => {
    await renderPage();
    expect(headers(annotatorsTable())).toEqual(['Annotator', 'Assigned documents']);
    const rows = annotatorsTable()
      .findRows()
      .map((r) => r.getElement().textContent);
    expect(rows).toEqual(['a@example.com2', 'b@example.com0', 'gone@example.com (not listed)1']);
  });

  it('shows Document, Source, Queue, Priority, State and Actions in the Documents table', async () => {
    await renderPage();
    expect(itemsTable().findHeaderSlot()!.getElement().textContent).toContain('Documents');
    expect(headers(itemsTable()).slice(-6)).toEqual(['Document', 'Source', 'Queue', 'Priority', 'State', 'Actions']);
  });
});

describe('the queue name', () => {
  it('is reported for the breadcrumb', async () => {
    const onName = vi.fn();
    await renderPage(onName);
    expect(onName).toHaveBeenCalledWith('Default');
  });
});

describe('queueAnnotatorRows', () => {
  it('puts the listed Annotators first, then anyone else holding items', () => {
    expect(
      queueAnnotatorRows(
        ['B@x.com', 'a@x.com'],
        [
          { email: 'c@x.com', claimed: 4 },
          { email: 'b@x.com', claimed: 1 },
        ],
      ),
    ).toEqual([
      { email: 'b@x.com', assigned: 1, listed: true },
      { email: 'a@x.com', assigned: 0, listed: true },
      { email: 'c@x.com', assigned: 4, listed: false },
    ]);
  });
});
