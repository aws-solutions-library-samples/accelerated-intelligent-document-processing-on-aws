// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * An Author on a Test Studio item, over the real adapter and edit flags. What has to
 * hold: an Author edits authored ground truth once Start editing has opened the version
 * transition; Save keeps a draft through the server (saveTestSetDocumentSectionDraft) and
 * Process Changes applies it (saveTestSetDocumentSection), never straight to S3; a document with a review copy stays view-only for an Author, whose
 * completion (completeSectionReview) excludes them, and says why.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
let editorProps: Record<string, unknown> | null = null;
const queue: { doc: Record<string, unknown>; draftVersion: number | null } = { doc: {}, draftVersion: null };

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({
  default: () => ({ setErrorMessage: vi.fn(), setSuccessMessage: vi.fn(), user: { username: 'au' } }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { TestSetBucket: 'test-set-bucket' } }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({
    isAdmin: false,
    isAuthor: true,
    isReviewer: false,
    isAnnotator: false,
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
  completeSectionReview,
  getAnnotationQueueDocument,
  openTestSetAnnotationDraft,
  saveTestSetDocumentSection,
  saveTestSetDocumentSectionDraft,
  uploadDocument,
} from '../../../graphql/generated';
import TestSetItemEdit from '../TestSetItemEdit';

const sent = () => graphql.mock.calls.map(([arg]) => (arg as { query: unknown }).query);

beforeEach(() => {
  document.body.innerHTML = '';
  editorProps = null;
  queue.draftVersion = null;
  queue.doc = {};
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve('{}') })) as never;
  graphql.mockReset().mockImplementation(async ({ query }: { query: unknown }) => {
    if (query === getAnnotationQueueDocument) {
      return {
        data: {
          getAnnotationQueue: {
            testSetId: 'ts-1',
            name: 'Invoices',
            skipReasons: [],
            draftVersion: queue.draftVersion,
            baseVersion: queue.draftVersion ? queue.draftVersion - 1 : null,
            documents: [
              {
                objectKey: 'a.pdf',
                inputKey: 'ts-1/input/a.pdf',
                claimedByMe: false,
                assignedTo: null,
                reviewed: false,
                skipped: false,
                labelSource: 'uploaded',
                reviewObjectKey: null,
                sections: [{ sectionId: '1', baselineKey: 'ts-1/baseline/a.pdf/sections/1/result.json', pageIndices: [0] }],
                ...queue.doc,
              },
            ],
          },
        },
      };
    }
    if (query === openTestSetAnnotationDraft) {
      queue.draftVersion = 2;
      return { data: { openTestSetAnnotationDraft: { baseVersion: 1, draftVersion: 2 } } };
    }
    if (String(query).includes('getFilePresignedUrl')) return { data: { getFilePresignedUrl: { presignedUrl: 'https://signed' } } };
    if (query === saveTestSetDocumentSectionDraft) {
      return {
        data: {
          saveTestSetDocumentSectionDraft: { sectionId: '1', revision: 1, draftUri: 's3://tsb/d1', updatedAt: 't', updatedBy: 'au' },
        },
      };
    }
    if (query === saveTestSetDocumentSection) return { data: { saveTestSetDocumentSection: { reviewed: true } } };
    return { data: {} };
  });
});

const renderItem = () =>
  render(
    <MemoryRouter>
      <TestSetItemEdit testSetId="ts-1" objectKey="a.pdf" />
    </MemoryRouter>,
  );

describe('an Author on a Test Studio item', () => {
  it('edits authored ground truth after Start editing, saving a draft and applying it through the server', async () => {
    renderItem();
    await waitFor(() => expect(editorProps).not.toBeNull());
    // Read-only until the version transition is open.
    expect(editorProps!.isReadOnly).toBe(true);
    const start = createWrapper(document.body)
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Start editing')!;
    await act(async () => start.click());
    await waitFor(() => expect(editorProps!.isReadOnly).toBe(false));

    await act(async () => {
      await (editorProps!.onSave as (id: string, d: Record<string, unknown>) => Promise<unknown>)('1', { inference_result: { Total: 1 } });
    });
    const draft = graphql.mock.calls.find(([arg]) => (arg as { query: unknown }).query === saveTestSetDocumentSectionDraft);
    expect(draft?.[0].variables).toMatchObject({ testSetId: 'ts-1', objectKey: 'a.pdf', sectionId: '1', baseRevision: 0 });
    expect(sent()).not.toContain(saveTestSetDocumentSection);

    const primary = createWrapper(document.body)
      .findHeader()!
      .findActions()!
      .findAllButtons()
      .find((b) => (b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent) === 'Process Changes')!;
    await act(async () => primary.click());
    await waitFor(() => expect(sent()).toContain(saveTestSetDocumentSection));
    expect(sent()).not.toContain(uploadDocument);
    expect(sent()).not.toContain(completeSectionReview);
  });

  it('keeps a document with a review copy view-only, saying why', async () => {
    queue.draftVersion = 2;
    queue.doc = { reviewObjectKey: 'run/a.pdf', labelSource: 'draft-machine' };
    renderItem();
    await waitFor(() => expect(editorProps).not.toBeNull());
    expect(editorProps!.isReadOnly).toBe(true);
    const primary = createWrapper(document.body)
      .findHeader()!
      .findActions()!
      .findAllButtons()
      .find((b) => (b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent) === 'Process Changes')!;
    primary.focus();
    expect(primary.findDisabledReason()?.getElement().textContent).toBe('Your role cannot save these labels');
  });
});
