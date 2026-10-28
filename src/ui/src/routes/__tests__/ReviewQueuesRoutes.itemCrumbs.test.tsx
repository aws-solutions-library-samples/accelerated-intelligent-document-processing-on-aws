// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit page's breadcrumbs on a Test Studio item. What has to hold: the shape is
 * Home › <test set> › <document> for every role; the set's crumb is its name once its
 * queue has loaded, linking to the set's queue page, with Home only until then. A
 * Production item's breadcrumbs are in document-edit-route.test.tsx (its route is the
 * document's edit page).
 */

import React from 'react';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const role = { isAdmin: true, isAnnotator: false, isAuthor: false, isReviewer: false, canAnnotate: true, canWrite: true, loading: false };
vi.mock('../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../contexts/app', () => ({
  default: () => ({
    navigationOpen: false,
    setNavigationOpen: vi.fn(),
    setErrorMessage: vi.fn(),
    setSuccessMessage: vi.fn(),
    user: { username: 'u' },
  }),
}));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../hooks/use-notifications', () => ({ default: () => [] }));
vi.mock('../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../hooks/use-page-thumbnails', () => ({ default: () => ({ urls: {}, failure: null }) }));
vi.mock('../../hooks/use-configuration', () => ({ default: () => ({ mergedConfig: null }) }));
vi.mock('../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
vi.mock('../../hooks/use-test-doc-pages', () => ({
  default: () => ({ pages: [], isLoading: false, error: null, previewUnavailable: false }),
}));
vi.mock('../../components/genaiidp-layout/navigation', () => ({ default: () => null }));
vi.mock('../../components/genai-idp-top-navigation', () => ({ default: () => null }));
vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../components/review-queues/ReviewTaskContext', () => ({ default: () => null }));
vi.mock('../../components/edit-screen/SectionEditor', () => ({ default: () => null }));
// The real screen, over its I/O adapter: it reports its test set once loaded.
const testSetName: { value: string | null | undefined } = { value: undefined };
vi.mock('../../components/edit-screen/test-set-item-edit-adapter', () => ({
  NOT_ASSIGNED_MESSAGE: 'not assigned',
  useTestSetItemEdit: (testSetId: string, objectKey: string) => {
    const queue =
      testSetName.value === undefined
        ? null
        : { testSetId, name: testSetName.value, skipReasons: [], documents: [{ objectKey, sections: [], claimedByMe: true }] };
    return {
      queue,
      doc: queue?.documents[0] ?? null,
      draft: null,
      loading: false,
      loadError: null,
      reload: vi.fn(),
      openDraft: vi.fn(),
      saveDraft: vi.fn(),
      loadSection: vi.fn(),
      reloadToken: 0,
      applyAndComplete: vi.fn(),
      skip: vi.fn(),
    };
  },
}));

import { TestSetItemPage } from '../ReviewQueuesRoutes';

const crumbs = () =>
  createWrapper(document.body)
    .findBreadcrumbGroup()!
    .findBreadcrumbLinks()
    .map((l) => ({ text: l.getElement().textContent, href: l.getElement().getAttribute('href') }));

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
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

describe('a Test Studio item’s breadcrumbs', () => {
  const TEST_ITEM = `/review-queues/test-sets/ts-1/items/${encodeURIComponent('in/a.pdf')}`;

  it('name the test set once its queue has loaded', async () => {
    testSetName.value = 'Invoices';
    renderAt(TEST_ITEM);
    await waitFor(() => expect(crumbs().map((c) => c.text)).toEqual(['Home', 'Invoices', 'a.pdf']));
    expect(crumbs()[1].href).toBe('#/review-queues/test-sets/ts-1');
  });

  it('show Home only until the queue loads', () => {
    testSetName.value = undefined;
    role.isAdmin = false;
    role.isAnnotator = true;
    renderAt(TEST_ITEM);
    expect(crumbs().map((c) => c.text)).toEqual(['Home']);
  });
});
