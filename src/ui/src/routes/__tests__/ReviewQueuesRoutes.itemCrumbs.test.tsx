// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The unified editor's breadcrumbs in the queue contexts. What has to hold: the shape
 * is Home › <queue or test set> › <document> for every role; a production item's queue
 * crumb is named as people see it and links to that queue's page once the item has
 * loaded; and a Test Studio item's crumb is the set's name once its queue has loaded,
 * its id until then, linking to the set's queue page.
 */

import React, { useEffect } from 'react';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const role = { isAdmin: true, isAnnotator: false, loading: false };
vi.mock('../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../contexts/app', () => ({ default: () => ({ navigationOpen: false, setNavigationOpen: vi.fn() }) }));
vi.mock('../../hooks/use-notifications', () => ({ default: () => [] }));
vi.mock('../../components/genaiidp-layout/navigation', () => ({ default: () => null }));
vi.mock('../../components/genai-idp-top-navigation', () => ({ default: () => null }));
vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
// The screen reports its item's queue (or test set) once it has loaded.
const testSetName: { value: string | null | undefined } = { value: undefined };
vi.mock('../../components/review-workspace/AnnotationScreen', () => ({
  default: ({
    onItemQueue,
    onTestSet,
  }: {
    onItemQueue?: (q: { id: string; name: string }) => void;
    onTestSet?: (t: { id: string; name: string | null }) => void;
  }) => {
    useEffect(() => {
      onItemQueue?.({ id: 'high value', name: 'High Value' });
      if (testSetName.value !== undefined) onTestSet?.({ id: 'ts-1', name: testSetName.value });
    }, [onItemQueue, onTestSet]);
    return null;
  },
}));

import { ProductionItemPage, TestSetItemPage } from '../ReviewQueuesRoutes';

const crumbs = () =>
  createWrapper(document.body)
    .findBreadcrumbGroup()!
    .findBreadcrumbLinks()
    .map((l) => ({ text: l.getElement().textContent, href: l.getElement().getAttribute('href') }));

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/review-queues/items/:objectKey" element={<ProductionItemPage />} />
        <Route path="/review-queues/test-sets/:testSetId/items/:objectKey" element={<TestSetItemPage />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  document.body.innerHTML = '';
  testSetName.value = undefined;
  role.isAdmin = true;
  role.isAnnotator = false;
});

const ITEM = `/review-queues/items/${encodeURIComponent('in/W2_input-8.pdf')}`;

describe('a production item’s breadcrumbs', () => {
  it('are Home › queue › document for an Admin, linking to the queue’s page', async () => {
    renderAt(ITEM);
    await waitFor(() => expect(crumbs().map((c) => c.text)).toEqual(['Home', 'High Value', 'W2_input-8.pdf']));
    expect(crumbs()[1].href).toBe('#/review-queues/queues/high%20value');
  });

  it('have the same shape for an Annotator', async () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    renderAt(ITEM);
    await waitFor(() => expect(crumbs().map((c) => c.text)).toEqual(['Home', 'High Value', 'W2_input-8.pdf']));
    expect(crumbs()[1].href).toBe('#/review-queues/queues/high%20value');
  });
});

describe('a Test Studio item’s breadcrumbs', () => {
  const TEST_ITEM = `/review-queues/test-sets/ts-1/items/${encodeURIComponent('in/a.pdf')}`;

  it('name the test set once its queue has loaded', async () => {
    testSetName.value = 'Invoices';
    renderAt(TEST_ITEM);
    await waitFor(() => expect(crumbs().map((c) => c.text)).toEqual(['Home', 'Invoices', 'a.pdf']));
    expect(crumbs()[1].href).toBe('#/review-queues/test-sets/ts-1');
  });

  it('show the test set id before the name loads', () => {
    testSetName.value = undefined;
    role.isAdmin = false;
    role.isAnnotator = true;
    renderAt(TEST_ITEM);
    expect(crumbs().map((c) => c.text)).toEqual(['Home', 'ts-1', 'a.pdf']);
    expect(crumbs()[1].href).toBe('#/review-queues/test-sets/ts-1');
  });
});
