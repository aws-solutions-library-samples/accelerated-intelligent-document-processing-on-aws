// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Drafts on a Test Studio item, over the real model and adapter. What has to hold: a
 * section with a draft opens from the draft rather than its label, in any later
 * session (the queue's `draftSections`); a save that lost to a newer draft says so,
 * re-reads the document and re-opens the section; and Skip sends only the skip, so the
 * drafts are kept for whoever picks the document up next.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { act, render, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
const setErrorMessage = vi.fn();
let editorProps: Record<string, unknown> | null = null;

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage, setSuccessMessage: vi.fn(), user: { username: 'ann' } }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { TestSetBucket: 'tsb' } }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({
    isAdmin: false,
    isAuthor: false,
    isReviewer: false,
    isAnnotator: true,
    canAnnotate: true,
    loading: false,
    allowedConfigVersions: null,
  }),
}));
vi.mock('../../../hooks/use-test-doc-pages', () => ({
  default: () => ({ pages: [], isLoading: false, error: null, previewUnavailable: false }),
}));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => ({ mergedConfig: null }) }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('../SectionEditor', () => ({
  default: (props: Record<string, unknown>) => {
    editorProps = props;
    return <div data-testid="editor" />;
  },
}));

import {
  getAnnotationQueueDocument,
  getFilePresignedUrl,
  saveTestSetDocumentSectionDraft,
  skipTestSetDocument,
} from '../../../graphql/generated';
import TestSetItemEdit from '../TestSetItemEdit';
import { STALE_DRAFT_MESSAGE, useTestSetItemEdit } from '../test-set-item-edit-adapter';

const DRAFT_URI = 's3://tsb/ts-1/drafts/a.pdf/sections/1/000002-ab.json';
const queueReply = () => ({
  data: {
    getAnnotationQueue: {
      testSetId: 'ts-1',
      name: 'Invoices',
      skipReasons: ['Out of time'],
      draftVersion: 2,
      baseVersion: 1,
      documents: [
        {
          objectKey: 'a.pdf',
          inputKey: 'ts-1/input/a.pdf',
          claimedByMe: true,
          assignedTo: 'ann@example.com',
          reviewed: false,
          skipped: false,
          labelSource: 'uploaded',
          reviewObjectKey: null,
          sections: [{ sectionId: '1', baselineKey: 'ts-1/baseline/a.pdf/sections/1/result.json', pageIndices: [0] }],
          draftSections: [{ sectionId: '1', revision: 2, draftUri: DRAFT_URI, updatedAt: 't', updatedBy: 'ann@example.com' }],
        },
      ],
    },
  },
});

const callsOf = (query: unknown) =>
  graphql.mock.calls.filter(([arg]) => (arg as { query: unknown }).query === query).map(([arg]) => arg.variables);

beforeEach(() => {
  document.body.innerHTML = '';
  editorProps = null;
  setErrorMessage.mockReset();
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve('{"inference_result":{"n":"draft"}}') })) as never;
  graphql.mockReset().mockImplementation(async ({ query }: { query: unknown }) => {
    if (query === getAnnotationQueueDocument) return queueReply();
    if (query === getFilePresignedUrl) return { data: { getFilePresignedUrl: { presignedUrl: 'https://signed' } } };
    return { data: {} };
  });
});

const renderItem = async () => {
  render(
    <MemoryRouter>
      <TestSetItemEdit testSetId="ts-1" objectKey="a.pdf" />
    </MemoryRouter>,
  );
  await waitFor(() => expect(editorProps).not.toBeNull());
};

describe('drafts on a Test Studio item', () => {
  it('opens a section from its draft, which a later session reads from the queue', async () => {
    await renderItem();
    let loaded: unknown;
    await act(async () => {
      loaded = await (editorProps!.loadSection as (id: string) => Promise<{ data: unknown }>)('1');
    });
    expect(callsOf(getFilePresignedUrl)).toEqual([{ s3Uri: DRAFT_URI }]);
    expect((loaded as { data: unknown }).data).toEqual({ inference_result: { n: 'draft' } });
  });

  it('says a newer draft was saved, re-reads the document and re-opens the section', async () => {
    await renderItem();
    const reads = callsOf(getAnnotationQueueDocument).length;
    const token = editorProps!.reloadToken as number;
    graphql.mockImplementation(async ({ query }: { query: unknown }) => {
      if (query === saveTestSetDocumentSectionDraft)
        throw { errors: [{ message: 'STALE_DRAFT: a newer draft of this section was saved' }] };
      if (query === getAnnotationQueueDocument) return queueReply();
      return { data: {} };
    });
    await act(async () => {
      await (editorProps!.onSave as (id: string, d: Record<string, unknown>) => Promise<unknown>)('1', { n: 'mine' });
    });
    expect(callsOf(saveTestSetDocumentSectionDraft)[0]).toMatchObject({ sectionId: '1', baseRevision: 2 });
    expect(setErrorMessage).toHaveBeenCalledWith(STALE_DRAFT_MESSAGE);
    expect(callsOf(getAnnotationQueueDocument).length).toBe(reads + 1);
    await waitFor(() => expect(editorProps!.reloadToken).toBe(token + 1));
  });

  it('skips with the skip alone, keeping the drafts', async () => {
    const { result } = renderHook(() => useTestSetItemEdit('ts-1', 'a.pdf'));
    await waitFor(() => expect(result.current.doc).not.toBeNull());
    graphql.mockClear();
    await act(() => result.current.skip('Out of time'));
    expect(graphql.mock.calls.map(([arg]) => (arg as { query: unknown }).query)).toEqual([skipTestSetDocument]);
  });
});
