// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A section with no field values says so.
 *
 * Reported from a live stack: the Visual Editor showed a "Document Data" heading with
 * nothing under it and no explanation, indistinguishable from the renderer having
 * failed. `{}` is truthy, so an empty `inference_result` passed the `inferenceResult ?`
 * gate and rendered an empty tree — while a *missing* one got a helpful alert.
 *
 * Two things reach this state, and they want different advice:
 *   - extraction ran and produced nothing (the reported case: a draft-labeling run over
 *     one section of a two-section document);
 *   - a section added by re-grouping, which the resolver writes with an empty
 *     `inference_result` on purpose, since nothing has extracted those pages as a group.
 */

import { render, screen, waitFor } from '@testing-library/react';
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
import type { TestSetDocumentSectionRef } from '../GroundTruthVisualEditor';

const SECTIONS: TestSetDocumentSectionRef[] = [
  { sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: 'Invoice', pageIndices: [0] },
];
const TWO_SECTIONS = [
  ...SECTIONS,
  { sectionId: '2', baselineKey: 'ts1/baseline/p.pdf/sections/2/result.json', documentClass: 'Invoice', pageIndices: [1] },
];

const mockBaseline = (baseline: Record<string, unknown>) => {
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve(JSON.stringify(baseline)) })) as never;
};

beforeEach(() => {
  graphql.mockReset();
  graphql.mockImplementation((args: { query: string }) => {
    if (args.query === 'getFilePresignedUrl') {
      return Promise.resolve({ data: { getFilePresignedUrl: { presignedUrl: 'https://example.test/signed' } } });
    }
    return Promise.resolve({ data: {} });
  });
});

const renderEditor = (sections: TestSetDocumentSectionRef[] = SECTIONS) =>
  render(
    <GroundTruthVisualEditor
      bucket="test-set-bucket"
      inputKey="ts1/input/p.pdf"
      objectKey="p.pdf"
      sections={sections}
      isReadOnly={false}
      testSetId="ts1"
    />,
  );

describe('GroundTruthVisualEditor with no field values', () => {
  it('explains an empty result instead of rendering a bare heading', async () => {
    // The reported bug: `{}` is truthy, so this used to reach the field renderer and
    // produce "Document Data" with no children and nothing to explain it.
    mockBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0] }, inference_result: {} });
    renderEditor();

    expect(await screen.findByText(/No field values for this section/i)).toBeInTheDocument();
    expect(screen.getByText(/extraction ran but produced no fields/i)).toBeInTheDocument();
    // And the heading it used to show alone is gone.
    expect(screen.queryByText('Document Data')).not.toBeInTheDocument();
  });

  it('names re-grouping as the other way a classified section gets here', async () => {
    // updateTestSetDocumentSections writes `inference_result: {}` for an added section
    // deliberately, so this is a state the product creates, not only a failure.
    //
    // Needs a class: an unclassified section has a more specific cause and takes the
    // branch below. This fixture originally had none, so it was passing for that reason
    // rather than the one it names.
    mockBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0] }, inference_result: {} });
    renderEditor();

    expect(await screen.findByText(/re-grouped/i)).toBeInTheDocument();
  });

  it('blames the missing class when there is one, and says re-extracting will not help', async () => {
    // The live case: the labelling run left section 1 unclassified, so there is no schema
    // and extraction has nothing to extract against. Telling a reviewer to re-extract
    // sends them round a loop that cannot succeed until a class is set, so the advice has
    // to differ from the other two causes.
    mockBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    renderEditor();

    expect(await screen.findByText(/no document class/i)).toBeInTheDocument();
    expect(screen.getByText(/no schema to extract against/i)).toBeInTheDocument();
    expect(screen.getByText(/re-extracting before that will not produce anything/i)).toBeInTheDocument();
    // And it offers the other real remedy: the pages may belong with another section.
    expect(screen.getByText(/can merge them/i)).toBeInTheDocument();
  });

  it('offers the class control on a section that has no class', async () => {
    // The dead end this pair of fixes exists for: the Class label field was gated on a
    // class already being set, so the section that needed one had no control to set it —
    // while the alert told the reviewer to change the class.
    mockBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    renderEditor();

    expect(await screen.findByText('Class label')).toBeInTheDocument();
    // Asserted as text, not getByPlaceholderText: a Cloudscape Select renders its
    // placeholder as button content, not an HTML placeholder attribute.
    expect(screen.getByText('Choose a document class')).toBeInTheDocument();
  });

  it('still distinguishes a missing result from an empty one', async () => {
    mockBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0] } });
    renderEditor();

    expect(await screen.findByText(/no inference_result at all/i)).toBeInTheDocument();
  });

  it('points at the other sections when the document has more than one', async () => {
    // The reported case exactly: two sections, one empty. Without this the reviewer has
    // no reason to think the document has data anywhere.
    mockBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    renderEditor(TWO_SECTIONS);

    expect(await screen.findByText(/Other sections of this document may still have values/i)).toBeInTheDocument();
  });

  it('says nothing about other sections on a single-section document', async () => {
    mockBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    renderEditor();

    await waitFor(() => expect(screen.getByText(/No field values for this section/i)).toBeInTheDocument());
    expect(screen.queryByText(/Other sections of this document/i)).not.toBeInTheDocument();
  });

  it('marks an unclassified section in its tab, and names every class from the payload', async () => {
    // A bare "Section 1" beside "Section 2 (Invoice)" expressed the missing
    // class only as absent text, which reads as "this tab shows the number". And only the
    // OPEN tab used to name its class at all, so finding the problem section meant
    // clicking each one — even though the payload carries documentClass for all of them.
    mockBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    renderEditor([
      { sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: null, pageIndices: [0] },
      { sectionId: '2', baselineKey: 'ts1/baseline/p.pdf/sections/2/result.json', documentClass: 'Invoice', pageIndices: [1] },
    ]);

    // findAllByText: a Cloudscape SegmentedControl renders each label more than once
    // (visible plus assistive copies), so the singular query is ambiguous by design.
    expect(await screen.findAllByText('Section 1 (no class)')).not.toHaveLength(0);
    // Named without being opened.
    expect(screen.getAllByText('Section 2 (Invoice)')).not.toHaveLength(0);
  });

  it('renders the fields normally when there are any', async () => {
    mockBaseline({ split_document: { page_indices: [0] }, inference_result: { total: '10.00' } });
    renderEditor();

    await waitFor(() => expect(screen.getByDisplayValue('10.00')).toBeInTheDocument());
    expect(screen.queryByText(/No field values for this section/i)).not.toBeInTheDocument();
  });
});
