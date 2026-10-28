// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Test Studio adapter's server calls. What has to hold: Save sends the draft
 * operation, naming the revision it edits, and never a reviewing operation or a direct S3
 * write; and completing applies every section in turn, a section's draft where it has
 * one and its labels as they are otherwise — through completeSectionReview, on the
 * review copy's key, for a document with a review copy, and through
 * saveTestSetDocumentSection for authored ground truth — reporting the server's
 * reviewed verdict.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage: vi.fn() }) }));

import {
  completeSectionReview,
  getAnnotationQueueDocument,
  getFilePresignedUrl,
  saveTestSetDocumentSection,
  saveTestSetDocumentSectionDraft,
  uploadDocument,
} from '../../../graphql/generated';
import { useTestSetItemEdit } from '../test-set-item-edit-adapter';

const SECTIONS = [
  { sectionId: '1', baselineKey: 'b1' },
  { sectionId: '2', baselineKey: 'b2' },
];
const DRAFT = {
  sectionId: '2',
  revision: 3,
  draftUri: 's3://tsb/ts-1/drafts/a.pdf/sections/2/000003-x.json',
  updatedAt: 't',
  updatedBy: 'u',
};

const queueWith = (doc: Record<string, unknown>) => ({
  data: {
    getAnnotationQueue: {
      testSetId: 'ts-1',
      name: 'Invoices',
      documents: [{ objectKey: 'a.pdf', inputKey: 'ts-1/input/a.pdf', sections: SECTIONS, draftSections: [DRAFT], ...doc }],
    },
  },
});

/** The queries sent, in order. */
const sent = () => graphql.mock.calls.map(([arg]) => (arg as { query: unknown }).query);
const callsOf = (query: unknown) =>
  graphql.mock.calls.filter(([arg]) => (arg as { query: unknown }).query === query).map(([arg]) => arg.variables);

const renderAdapter = async (doc: Record<string, unknown>) => {
  graphql.mockImplementation(async ({ query }: { query: unknown }) => {
    if (query === getAnnotationQueueDocument) return queueWith(doc);
    if (query === getFilePresignedUrl) return { data: { getFilePresignedUrl: { presignedUrl: 'https://signed' } } };
    if (query === saveTestSetDocumentSectionDraft) {
      return { data: { saveTestSetDocumentSectionDraft: { ...DRAFT, sectionId: '1', revision: 1, draftUri: 's3://tsb/d1' } } };
    }
    if (query === saveTestSetDocumentSection) return { data: { saveTestSetDocumentSection: { reviewed: true } } };
    return { data: {} };
  });
  const hook = renderHook(() => useTestSetItemEdit('ts-1', 'a.pdf'));
  await waitFor(() => expect(hook.result.current.doc).not.toBeNull());
  return hook;
};

beforeEach(() => {
  graphql.mockReset();
  global.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, text: () => Promise.resolve(JSON.stringify({ inference_result: { n: 'draft' } })) }),
  ) as never;
});

describe('the Test Studio adapter', () => {
  it('saves a draft, naming the revision it edits, and nothing that reviews', async () => {
    const { result } = await renderAdapter({ reviewObjectKey: 'run/a.pdf' });
    await act(() => result.current.saveDraft('2', { inference_result: { Total: 2 } }));
    await act(() => result.current.saveDraft('1', { inference_result: { Total: 3 } }));
    await act(() => result.current.saveDraft('1', { inference_result: { Total: 4 } }));
    expect(callsOf(saveTestSetDocumentSectionDraft).map((v) => [v.sectionId, v.baseRevision])).toEqual([
      ['2', 3],
      ['1', 0],
      ['1', 1],
    ]);
    expect(sent()).not.toContain(completeSectionReview);
    expect(sent()).not.toContain(saveTestSetDocumentSection);
    expect(sent()).not.toContain(uploadDocument);
  });

  it('opens a section from its draft when it has one', async () => {
    const { result } = await renderAdapter({ reviewObjectKey: null });
    const fallback = vi.fn(async () => ({ data: { label: true }, raw: '{}' }));
    await act(async () => {
      await result.current.loadSection('2', fallback);
      await result.current.loadSection('1', fallback);
    });
    expect(callsOf(getFilePresignedUrl)).toEqual([{ s3Uri: DRAFT.draftUri }]);
    expect(fallback).toHaveBeenCalledWith('1');
    expect(fallback).toHaveBeenCalledTimes(1);
  });

  it('applies a review copy through completeSectionReview, every section in order, the draft as the edit', async () => {
    const { result } = await renderAdapter({ reviewObjectKey: 'run/a.pdf', reviewed: true });
    let outcome: { reviewed: boolean } | undefined;
    await act(async () => {
      outcome = await result.current.applyAndComplete();
    });
    expect(callsOf(completeSectionReview)).toEqual([
      { objectKey: 'run/a.pdf', sectionId: '1' },
      { objectKey: 'run/a.pdf', sectionId: '2', editedData: JSON.stringify({ inference_result: { n: 'draft' } }) },
    ]);
    expect(sent()).not.toContain(saveTestSetDocumentSection);
    expect(outcome).toEqual({ reviewed: true });
  });

  it('applies authored ground truth through saveTestSetDocumentSection, and reports its verdict', async () => {
    const { result } = await renderAdapter({ reviewObjectKey: null, labelSource: 'uploaded' });
    let outcome: { reviewed: boolean } | undefined;
    await act(async () => {
      outcome = await result.current.applyAndComplete();
    });
    expect(callsOf(saveTestSetDocumentSection)).toEqual([
      { testSetId: 'ts-1', objectKey: 'a.pdf', sectionId: '1' },
      { testSetId: 'ts-1', objectKey: 'a.pdf', sectionId: '2', data: JSON.stringify({ inference_result: { n: 'draft' } }) },
    ]);
    expect(sent()).not.toContain(completeSectionReview);
    expect(sent()).not.toContain(uploadDocument);
    expect(outcome).toEqual({ reviewed: true });
  });
});
