// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Home › Tasks: the table shows Document, Queue, Age and Reason, then the row menu, and
 * offers Source and Priority in its settings, kept in the browser under the page's own
 * key. The reason of a production item is the reason its routing rule gave.
 */

import React from 'react';
import { act } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import type { ReviewItem, TestSetTask } from '../../../graphql/generated/operation-types';
import { fromTestSetTask } from '../work-items';

const work = vi.hoisted(() => ({ value: null as Record<string, unknown> | null }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => work.value }));

import MyTasksPage from '../MyTasksPage';

const ITEM = {
  objectKey: 'in/invoice.pdf',
  cycleId: 'c1',
  profile: 'default',
  queue: 'claims',
  queueName: 'Claims team',
  priority: 50,
  state: 'CLAIMED',
  enqueuedAt: '2026-10-22T00:00:00.000Z',
  reasons: [],
  reasonCount: 0,
  isMine: true,
  routedBy: {
    ruleId: 'claims-over-1000',
    ruleLabel: 'Claims over 1000',
    fallbackReason: '',
    reason: 'Invoice total 1250.00 is over 1000.',
  },
} as unknown as ReviewItem;

const table = () => createWrapper(document.body).findTable()!;
const headers = (): (string | null)[] =>
  table()
    .findColumnHeaders()
    .map((header) => header.getElement().textContent);

const renderPage = () =>
  renderWithIntl(
    <MemoryRouter>
      <MyTasksPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  localStorage.clear();
  work.value = {
    myItems: [ITEM],
    testSetTasks: [fromTestSetTask({ testSetId: 'set-1', objectKey: 'set-1/doc.pdf', alertCount: 2 } as TestSetTask)],
    count: 2,
    loaded: true,
    pollError: null,
    refresh: async () => undefined,
  };
});

describe('Home › Tasks', () => {
  it('shows Document, Queue, Age and Reason, then the row menu', () => {
    renderPage();

    expect(headers()).toEqual(['Document', 'Queue', 'Age', 'Reason', 'Actions']);
    expect(table().findRows()).toHaveLength(2);
  });

  it('shows the reason the routing rule gave for a production item, and the alerts of a Test Studio document', () => {
    renderPage();

    expect(table().findBodyCell(1, 4)!.getElement().textContent).toBe('Invoice total 1250.00 is over 1000.');
    expect(table().findBodyCell(2, 4)!.getElement().textContent).toBe('2 confidence alerts');
  });

  it('offers Source and Priority in its settings, and keeps the choice under its own key', () => {
    renderPage();
    const preferences = table().findCollectionPreferences()!;
    act(() => preferences.findTriggerButton().click());
    const options = preferences.findModal()!.findContentDisplayPreference()!.findOptions();

    expect(options.map((option) => option.findLabel().getElement().textContent)).toEqual([
      'Document',
      'Source',
      'Queue',
      'Priority',
      'Age',
      'Reason',
    ]);

    act(() => options[1].findVisibilityToggle().findNativeInput().click());
    act(() => preferences.findModal()!.findConfirmButton()!.click());

    expect(headers()).toEqual(['Document', 'Source', 'Queue', 'Age', 'Reason', 'Actions']);
    expect(JSON.parse(localStorage.getItem('my-tasks-preferences')!).contentDisplay).toContainEqual({ id: 'source', visible: true });
    // The queue page keeps its own settings.
    expect(localStorage.getItem('review-queue-items-preferences')).toBeNull();
  });
});
