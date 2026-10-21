// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The annotation screens' breadcrumbs. What has to hold: a production item sits under
 * its queue once the item has loaded — Home › Queue Management › <queue> › <document>
 * for an Admin, Home › My Work › Queues › <queue> › <document> for an Annotator — the
 * queue crumb named as people see it and linking to that queue's page; and a Test
 * Studio item sits under its test set the same way for both roles.
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
// The screen reports its item's queue once the item has loaded.
vi.mock('../../components/review-workspace/AnnotationScreen', () => ({
  default: ({ onItemQueue }: { onItemQueue?: (q: { id: string; name: string }) => void }) => {
    useEffect(() => {
      onItemQueue?.({ id: 'high value', name: 'High Value' });
    }, [onItemQueue]);
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
  role.isAdmin = true;
  role.isAnnotator = false;
});

const ITEM = `/review-queues/items/${encodeURIComponent('in/W2_input-8.pdf')}`;

describe('a production item’s breadcrumbs', () => {
  it('name its queue under Queue Management for an Admin, linking to the queue’s page', async () => {
    renderAt(ITEM);
    await waitFor(() => expect(crumbs()).toHaveLength(4));
    expect(crumbs().map((c) => c.text)).toEqual(['Home', 'Queue Management', 'High Value', 'W2_input-8.pdf']);
    expect(crumbs()[2].href).toBe('#/review-queues/queues/high%20value');
  });

  it('name its queue under My Work › Queues for an Annotator', async () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    renderAt(ITEM);
    await waitFor(() => expect(crumbs()).toHaveLength(5));
    expect(crumbs().map((c) => c.text)).toEqual(['Home', 'My Work', 'Queues', 'High Value', 'W2_input-8.pdf']);
    expect(crumbs()[2].href).toBe('#/my-work/queues');
    expect(crumbs()[3].href).toBe('#/review-queues/queues/high%20value');
  });
});

describe('a Test Studio item’s breadcrumbs', () => {
  const TEST_ITEM = `/review-queues/test-sets/ts-1/items/${encodeURIComponent('in/a.pdf')}`;

  it('name its test set under Queue Management for an Admin', () => {
    renderAt(TEST_ITEM);
    expect(crumbs().map((c) => c.text)).toEqual(['Home', 'Queue Management', 'Test set: ts-1', 'a.pdf']);
    expect(crumbs()[2].href).toBe('#/review-queues/test-sets/ts-1');
  });

  it('name its test set under My Work › Queues for an Annotator', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    renderAt(TEST_ITEM);
    expect(crumbs().map((c) => c.text)).toEqual(['Home', 'My Work', 'Queues', 'Test set: ts-1', 'a.pdf']);
    expect(crumbs()[3].href).toBe('#/review-queues/test-sets/ts-1');
  });
});
