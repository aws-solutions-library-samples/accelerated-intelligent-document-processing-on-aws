// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit route, #/documents/<key>/edit. What has to hold: the mode the resolver chose
 * is the screen shown — the Production item (seeded with what was read) for a queue
 * decision, the direct edit (with its lock) otherwise; while the mode resolves the
 * breadcrumbs are Home only; an Annotator-only user who is not a member is told so,
 * with a link to Home › Queues; a key with `%` reaches the page decoded exactly once;
 * #/documents/<key> still opens the document page; and choosing the mode costs no
 * read beyond the document and the item (the item's poll then waits its interval).
 */

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EditModeState } from '../../components/edit-screen/use-edit-mode';

const state = vi.hoisted(() => ({
  mode: { status: 'loading' } as unknown,
  keys: [] as string[],
  realMode: false,
  role: { loading: false, isAdmin: true, isAnnotator: false, isAnnotatorOnly: false, canWorkReviewQueues: true, canWrite: true },
}));
const graphql = vi.fn();

vi.mock('../../contexts/app', () => ({
  default: () => ({
    navigationOpen: false,
    setNavigationOpen: vi.fn(),
    setErrorMessage: vi.fn(),
    setSuccessMessage: vi.fn(),
    user: { username: 'u' },
  }),
}));
vi.mock('../../hooks/use-notifications', () => ({ default: () => [] }));
vi.mock('../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../components/genaiidp-layout/navigation', () => ({ default: () => null }));
vi.mock('../../components/genai-idp-top-navigation', () => ({ default: () => null }));
vi.mock('../../components/genaiidp-layout', () => ({ default: () => <div data-testid="document-page" /> }));
vi.mock('../../components/review-queues/ReviewTaskContext', () => ({ default: () => null }));
vi.mock('../../components/edit-screen/SectionEditor', () => ({ default: () => <div data-testid="editor" /> }));
vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../components/edit-screen/use-edit-mode', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../components/edit-screen/use-edit-mode')>();
  return {
    ...real,
    default: (objectKey: string) => {
      state.keys.push(objectKey);
      return state.realMode ? real.default(objectKey) : state.mode;
    },
  };
});
vi.mock('../../components/edit-screen/DocumentEdit', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../components/edit-screen/DocumentEdit')>();
  return {
    default: (props: { objectKey: string; seed?: { lock?: string } }) =>
      state.realMode ? (
        <real.default {...(props as unknown as React.ComponentProps<typeof real.default>)} />
      ) : (
        <div data-testid="direct-edit" data-key={props.objectKey} data-lock={props.seed?.lock ?? ''} />
      ),
  };
});
vi.mock('../../components/edit-screen/ProductionItemEdit', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../components/edit-screen/ProductionItemEdit')>();
  return {
    default: (props: { objectKey: string; seed?: { detail: { item: { state: string } } } }) =>
      state.realMode ? (
        <real.default {...(props as unknown as React.ComponentProps<typeof real.default>)} />
      ) : (
        <div data-testid="queue-edit" data-key={props.objectKey} data-state={props.seed?.detail.item.state ?? ''} />
      ),
  };
});

import { getDocument, getReviewItem } from '../../graphql/generated';
import DocumentsRoutes from '../DocumentsRoutes';

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/documents/*" element={<DocumentsRoutes />} />
      </Routes>
    </MemoryRouter>,
  );

const crumbs = () =>
  createWrapper(document.body)
    .findBreadcrumbGroup()!
    .findBreadcrumbLinks()
    .map((l) => l.getElement().textContent);

const DOC = { ObjectKey: 'a/b.pdf', HITLStatus: 'in_review', Sections: [], Pages: [] };
const DETAIL = {
  item: { objectKey: 'a/b.pdf', queue: 'default', state: 'OPEN', isMine: false, ownerEmail: null, reasonCount: 0 },
  queueName: 'Default',
  sections: [{ sectionId: '1', documentClass: 'W2', resultUri: 's3://out/a/b.pdf/sections/1/result.json', draftUri: null, pageIds: [] }],
  history: [],
  skipReasons: [],
};
const resolved = (decision: unknown): EditModeState => ({ status: 'resolved', decision, document: DOC, retry: vi.fn() }) as EditModeState;

beforeEach(() => {
  document.body.innerHTML = '';
  state.mode = { status: 'loading' };
  state.keys = [];
  state.realMode = false;
  graphql.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('the edit route', () => {
  it('opens the Production item, seeded with what was read, for a queue decision', () => {
    state.mode = resolved({ mode: 'queue', detail: DETAIL });
    renderAt('/documents/a%2Fb.pdf/edit');
    const shown = screen.getByTestId('queue-edit');
    expect(shown.getAttribute('data-key')).toBe('a/b.pdf');
    expect(shown.getAttribute('data-state')).toBe('OPEN');
    expect(screen.queryByTestId('direct-edit')).toBeNull();
  });

  it('opens the direct edit, with its lock, for a direct decision', () => {
    state.mode = resolved({ mode: 'direct', lock: 'mirror-lag' });
    renderAt('/documents/a%2Fb.pdf/edit');
    expect(screen.getByTestId('direct-edit').getAttribute('data-lock')).toBe('mirror-lag');
    expect(screen.queryByTestId('queue-edit')).toBeNull();
  });

  it('shows Home only in the breadcrumbs while the mode resolves', () => {
    renderAt('/documents/a%2Fb.pdf/edit');
    expect(crumbs()).toEqual(['Home']);
    expect(document.body.textContent).toContain('Loading document…');
  });

  it('tells an Annotator-only user who is not a member, with a link to Home › Queues', () => {
    state.mode = resolved({ mode: 'not-a-member' });
    renderAt('/documents/a%2Fb.pdf/edit');
    expect(document.body.textContent).toContain("You can't open this review item");
    expect(document.querySelector('a[href="#/my-work/queues"]')).not.toBeNull();
  });

  it('decodes a key with % exactly once', () => {
    state.mode = resolved({ mode: 'direct' });
    expect(() => renderAt('/documents/100%25%2Foff.pdf/edit')).not.toThrow();
    expect(state.keys[0]).toBe('100%/off.pdf');
    expect(screen.getByTestId('direct-edit').getAttribute('data-key')).toBe('100%/off.pdf');
  });

  it('leaves the document page on its own route', () => {
    renderAt('/documents/a%2Fb.pdf');
    expect(screen.getByTestId('document-page')).toBeTruthy();
    expect(screen.queryByTestId('direct-edit')).toBeNull();
  });

  it('has the breadcrumbs Review Studio › Review Management › queue › document for an Admin’s queue item, linking to the queue', async () => {
    state.realMode = true;
    graphql.mockImplementation(async ({ query }: { query: unknown }) => {
      if (query === getDocument) return { data: { getDocument: DOC } };
      if (query === getReviewItem) return { data: { getReviewItem: DETAIL } };
      return { data: {} };
    });
    renderAt('/documents/a%2Fb.pdf/edit');
    expect(await screen.findByTestId('editor')).toBeTruthy();
    expect(crumbs()).toEqual(['Review Studio', 'Review Management', 'Default', 'b.pdf']);
    const queueCrumb = createWrapper(document.body).findBreadcrumbGroup()!.findBreadcrumbLinks()[2].getElement();
    expect(queueCrumb.getAttribute('href')).toBe('#/review-queues/queues/default');
  });

  it('has the document page’s trail for a queue item opened from the document page', async () => {
    state.realMode = true;
    graphql.mockImplementation(async ({ query }: { query: unknown }) => {
      if (query === getDocument) return { data: { getDocument: DOC } };
      if (query === getReviewItem) return { data: { getReviewItem: DETAIL } };
      return { data: {} };
    });
    renderAt('/documents/a%2Fb.pdf/edit?from=document');
    expect(await screen.findByTestId('editor')).toBeTruthy();
    expect(crumbs()).toEqual(['Document Processing', 'Documents', 'b.pdf', 'Edit']);
  });

  it('costs no read beyond the document and the item, and polls the item on its interval', async () => {
    vi.useFakeTimers();
    state.realMode = true;
    graphql.mockImplementation(async ({ query }: { query: unknown }) => {
      if (query === getDocument) return { data: { getDocument: DOC } };
      if (query === getReviewItem) return { data: { getReviewItem: DETAIL } };
      return { data: {} };
    });
    const count = (query: unknown) => graphql.mock.calls.filter(([arg]) => (arg as { query: unknown }).query === query).length;
    renderAt('/documents/a%2Fb.pdf/edit');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(screen.getByTestId('editor')).toBeTruthy();
    expect(count(getDocument)).toBe(1);
    expect(count(getReviewItem)).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(19_000);
    });
    expect(count(getReviewItem)).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(count(getReviewItem)).toBe(2);
    expect(count(getDocument)).toBe(1);
  });
});
