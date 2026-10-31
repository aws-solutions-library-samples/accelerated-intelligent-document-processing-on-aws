// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Old edit links keep working, permanently: Inbox messages keep their link for up to a
 * year, and copied field links never expire. What has to hold, per legacy path: the
 * redirect lands on the current route with the key encoded exactly once and the whole
 * query kept; a Production item's link redirects for every role (the edit page decides
 * what the caller sees); the retired Test Studio document detail page redirects to the
 * document's Test Studio item; and the older Test Studio annotate link still redirects.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const role = vi.hoisted(() => ({
  current: { loading: false, isAdmin: false, isAnnotator: false, isAuthor: true, groups: ['Author'] } as Record<string, unknown>,
}));
vi.mock('../../hooks/use-user-role', () => ({ default: () => role.current }));
vi.mock('../../components/genai-idp-top-navigation', () => ({ default: () => null }));
vi.mock('../../components/edit-screen/TestSetItemEdit', () => ({ default: () => null }));
vi.mock('../../components/test-studio/AnnotationWorkspace', () => ({ default: () => null }));
vi.mock('../../components/test-studio/TestSetDetail', () => ({ default: () => null }));
vi.mock('../../components/test-studio/TestStudioLayout', () => ({ default: () => null }));
vi.mock('../../components/test-studio/TestSetDocumentDetail', () => ({ default: () => null }));
vi.mock('../../components/review-queues/QueuesPage', () => ({ default: () => null }));
vi.mock('../../components/review-queues/ReviewQueuePage', () => ({ default: () => null }));
vi.mock('../../components/review-queues/ReviewQueuesLayout', () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { LEGACY_REVIEW_ITEM_PATH } from '../constants';
import ReviewQueuesRoutes from '../ReviewQueuesRoutes';
import TestStudioRoutes from '../TestStudioRoutes';

/** Where the redirect landed, exactly as the router holds it (still encoded). */
const Landed = (): React.JSX.Element => {
  const { pathname, search } = useLocation();
  return <div data-testid="landed" data-path={pathname} data-search={search} />;
};

/** `queuesRouted`: whether /review-queues is routed (off when the target is under it, so the probe sees it). */
const renderAt = (path: string, queuesRouted = true) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {queuesRouted && <Route path="/review-queues/*" element={<ReviewQueuesRoutes />} />}
        <Route path="/test-studio/*" element={<TestStudioRoutes />} />
        <Route path="*" element={<Landed />} />
      </Routes>
    </MemoryRouter>,
  );

const landed = () => {
  const el = screen.getByTestId('landed');
  return { path: el.getAttribute('data-path'), search: el.getAttribute('data-search') };
};

beforeEach(() => {
  document.body.innerHTML = '';
  role.current = { loading: false, isAdmin: false, isAnnotator: false, isAuthor: true, groups: ['Author'] };
});

describe('a Production item’s former link', () => {
  it('opens the document’s edit page, the key encoded once, keeping the whole query', () => {
    renderAt(`${LEGACY_REVIEW_ITEM_PATH}/a%2Fb%20c%25d.pdf?field=a.b&view=source&section=2`);
    expect(landed()).toEqual({ path: '/documents/a%2Fb%20c%25d.pdf/edit', search: '?field=a.b&view=source&section=2' });
  });

  it('opens it with no query', () => {
    renderAt(`${LEGACY_REVIEW_ITEM_PATH}/a%2Fb.pdf`);
    expect(landed()).toEqual({ path: '/documents/a%2Fb.pdf/edit', search: '' });
  });

  it('redirects for an Author too: the edit page decides what they see', () => {
    role.current = { loading: false, isAdmin: false, isAnnotator: false, isAuthor: true, groups: ['Author'] };
    renderAt(`${LEGACY_REVIEW_ITEM_PATH}/a.pdf`);
    expect(landed().path).toBe('/documents/a.pdf/edit');
  });
});

describe('the Test Studio annotate link', () => {
  it('still opens the document’s Test Studio item', () => {
    renderAt('/test-studio/sets/ts-1/annotate?doc=a%2Fb.pdf', false);
    expect(landed()).toEqual({ path: '/review-queues/test-sets/ts-1/items/a%2Fb.pdf', search: '' });
  });
});

describe('the retired Test Studio document detail link', () => {
  it('opens the document’s Test Studio item, the key encoded once, keeping view=source', () => {
    renderAt('/test-studio/sets/ts-1/doc/a/b%20c.pdf?view=source', false);
    expect(landed()).toEqual({ path: '/review-queues/test-sets/ts-1/items/a%2Fb%20c.pdf', search: '?view=source' });
  });

  it('drops view=ground-truth, which is the item’s default', () => {
    renderAt('/test-studio/sets/ts-1/doc/a.pdf?view=ground-truth', false);
    expect(landed()).toEqual({ path: '/review-queues/test-sets/ts-1/items/a.pdf', search: '' });
  });

  it('keeps a key with % as stored', () => {
    renderAt('/test-studio/sets/ts-1/doc/100%25off.pdf', false);
    expect(landed().path).toBe('/review-queues/test-sets/ts-1/items/100%25off.pdf');
  });
});
