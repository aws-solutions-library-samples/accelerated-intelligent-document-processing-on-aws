// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Who gets credited for a review, and on which save path.
 *
 * The bug: on a set that arrived with its own ground truth, correcting a document left
 * the annotation queue reporting "0 of 73 documents reviewed" no matter how many you
 * fixed — while the toast said the document had been marked reviewed. The server derives
 * `reviewed` from `labelSource == 'reviewed-human'` (test_set_resolver/index.py), and the
 * direct-to-S3 save wrote `_editHistory` but never `labelSource`.
 *
 * That path is not an edge case: `completeSectionReview` requires a `reviewObjectKey`,
 * which only a draft-labeling run creates, so an authored-ground-truth document *must*
 * save directly. It is exactly the kind of set the report came from.
 *
 * The distinction these tests pin is that the tag is written on the direct path and NOT
 * on the review-API path, where the server writes it with token-derived identity.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  getFilePresignedUrl: 'getFilePresignedUrl',
  uploadDocument: 'uploadDocument',
  reextractTestSetDocument: 'reextractTestSetDocument',
  getDraftLabelJob: 'getDraftLabelJob',
  updateTestSetDocumentSections: 'updateTestSetDocumentSections',
}));

vi.mock('../../../hooks/use-test-doc-pages', () => ({
  default: () => ({ pages: [{ Id: '1', ImageUri: 'blob:1' }], isLoading: false, error: null, previewUnavailable: false }),
}));
vi.mock('../../../hooks/use-configuration', () => ({
  default: () => ({ mergedConfig: { classes: [{ $id: 'Invoice' }] }, loading: false, error: null }),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ user: { username: 'tester' } }) }));

import GroundTruthVisualEditor from '../GroundTruthVisualEditor';

/** An uploaded baseline: authoritative, but with no labelSource of its own. */
const BASELINE = {
  document_class: { type: 'Invoice' },
  split_document: { page_indices: [0] },
  inference_result: { total: '10.00' },
};

const SECTIONS = [{ sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: 'Invoice', pageIndices: [0] }];

/** The JSON body handed to the presigned POST. */
let uploadedBody: Record<string, unknown> | null = null;

beforeEach(() => {
  graphql.mockReset();
  uploadedBody = null;
  global.fetch = vi.fn(async (_url: unknown, init?: { body?: FormData }) => {
    const file = init?.body?.get?.('file') as Blob | undefined;
    if (file) uploadedBody = JSON.parse(await file.text());
    return { ok: true, text: () => Promise.resolve(JSON.stringify(BASELINE)) };
  }) as never;
  graphql.mockImplementation((args: { query: string }) => {
    if (args.query === 'getFilePresignedUrl') {
      return Promise.resolve({ data: { getFilePresignedUrl: { presignedUrl: 'https://example.test/signed' } } });
    }
    if (args.query === 'uploadDocument') {
      return Promise.resolve({
        data: { uploadDocument: { presignedUrl: JSON.stringify({ url: 'https://s3.test/post', fields: {} }), usePostMethod: 'true' } },
      });
    }
    return Promise.resolve({ data: {} });
  });
});

const renderEditor = (props: Record<string, unknown> = {}) =>
  render(
    <GroundTruthVisualEditor
      bucket="test-set-bucket"
      inputKey="ts1/input/p.pdf"
      objectKey="p.pdf"
      sections={SECTIONS}
      isReadOnly={false}
      testSetId="ts1"
      {...props}
    />,
  );

const editAndSave = async () => {
  // The document has to load before its values can be edited or saved.
  await waitFor(() => expect(screen.getByDisplayValue('10.00')).toBeInTheDocument());
  await userEvent.clear(screen.getByDisplayValue('10.00'));
  await userEvent.type(screen.getByRole('textbox', { name: /total/i }), '99.00');
  await userEvent.click(screen.getByRole('button', { name: /^Save/i }));
};

describe('GroundTruthVisualEditor review provenance', () => {
  it('tags the label reviewed-human when it writes the baseline itself', async () => {
    renderEditor();
    await editAndSave();

    // Without this the queue counts the document as unreviewed forever, because
    // `reviewed` is an exact comparison against this string server-side.
    await waitFor(() => expect(uploadedBody).not.toBeNull());
    expect(uploadedBody!.labelSource).toBe('reviewed-human');
  });

  it('still records who edited it, alongside the tag', async () => {
    renderEditor();
    await editAndSave();

    await waitFor(() => expect(uploadedBody).not.toBeNull());
    const history = uploadedBody!._editHistory as { editedBy: string }[];
    expect(history[history.length - 1].editedBy).toBe('tester');
  });

  it('does not tag it when the review API is doing the saving', async () => {
    // completeSectionReview writes provenance server-side with token-derived identity;
    // tagging here as well would double-record, and would let the client assert an
    // identity the server never verified.
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderEditor({ onSave });
    await editAndSave();

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const saved = onSave.mock.calls[0][1] as Record<string, unknown>;
    expect(saved.labelSource).toBeUndefined();
    // And nothing went to S3 on this path.
    expect(graphql.mock.calls.some((c) => c[0].query === 'uploadDocument')).toBe(false);
  });
});
