// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The seam between the regrouping board and the baseline.
 *
 * The board works in 1-based page ids, because that is what `TestDocPage.Id` is and what
 * a reviewer reads off the page. The baseline stores 0-based `page_indices`. This file
 * exists for the conversion at that boundary: `section-grouping.test.ts` proves the two
 * helpers are correct in isolation, and this proves they are actually *applied*, in the
 * right direction, on the way in and on the way out.
 *
 * Getting that wrong would shift every section by one page and raise nothing — the
 * baseline would still be valid JSON with plausible numbers.
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

// Three rendered pages, 1-based ids, as use-test-doc-pages produces them.
vi.mock('../../../hooks/use-test-doc-pages', () => ({
  default: () => ({
    pages: [
      { Id: '1', ImageUri: 'blob:1' },
      { Id: '2', ImageUri: 'blob:2' },
      { Id: '3', ImageUri: 'blob:3' },
    ],
    isLoading: false,
    error: null,
    previewUnavailable: false,
  }),
}));

vi.mock('../../../hooks/use-configuration', () => ({
  default: () => ({
    mergedConfig: { classes: [{ $id: 'Invoice' }, { $id: 'W2' }] },
    loading: false,
    error: null,
  }),
}));

vi.mock('../../../contexts/app', () => ({ default: () => ({ user: { username: 'tester' } }) }));

import GroundTruthVisualEditor from '../GroundTruthVisualEditor';

/** The open section's baseline: pages 1-2 in the reviewer's terms, [0,1] on disk. */
const BASELINE = {
  document_class: { type: 'Invoice' },
  split_document: { page_indices: [0, 1] },
  inference_result: { total: '10.00' },
  labelSource: 'reviewed-human',
};

const SECTIONS = [
  { sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: 'Invoice', pageIndices: [0, 1] },
  { sectionId: '2', baselineKey: 'ts1/baseline/p.pdf/sections/2/result.json', documentClass: 'W2', pageIndices: [2] },
];

beforeEach(() => {
  graphql.mockReset();
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve(JSON.stringify(BASELINE)) })) as never;
  graphql.mockImplementation((args: { query: string }) => {
    if (args.query === 'getFilePresignedUrl') {
      return Promise.resolve({ data: { getFilePresignedUrl: { presignedUrl: 'https://example.test/signed' } } });
    }
    if (args.query === 'updateTestSetDocumentSections') {
      return Promise.resolve({
        data: {
          updateTestSetDocumentSections: {
            testSetId: 'ts1',
            objectKey: 'p.pdf',
            // Only the surviving section: the mutation deletes a removed one rather
            // than returning it empty.
            sections: [{ sectionId: '1', documentClass: 'Invoice', pageIndices: [0, 1, 2] }],
          },
        },
      });
    }
    return Promise.resolve({ data: {} });
  });
});

const renderEditor = () =>
  render(
    <GroundTruthVisualEditor
      bucket="test-set-bucket"
      inputKey="ts1/input/p.pdf"
      objectKey="p.pdf"
      sections={SECTIONS}
      isReadOnly={false}
      testSetId="ts1"
    />,
  );

const openBoard = async () => {
  await waitFor(() => expect(screen.getByRole('button', { name: /Edit page grouping/i })).toBeEnabled());
  await userEvent.click(screen.getByRole('button', { name: /Edit page grouping/i }));
};

describe('GroundTruthVisualEditor page regrouping', () => {
  it('shows the grouping in the reviewer 1-based terms, not the stored 0-based ones', async () => {
    renderEditor();

    // [0, 1] on disk is "1, 2" on screen. Showing raw indices was the old behaviour and
    // made the field read as though it described different pages.
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
  });

  it('opens the board with every section, converted to page ids', async () => {
    renderEditor();
    await openBoard();

    // Both sections, from the payload rather than a refetch — the editor only loads the
    // section being viewed.
    expect(screen.getByText('Section 1')).toBeInTheDocument();
    expect(screen.getByText('Section 2')).toBeInTheDocument();
    // Page 3 is index 2 on disk; it must appear as page 3.
    expect(screen.getByRole('checkbox', { name: /Select page 3/i })).toBeInTheDocument();
  });

  it('converts back to 0-based indices when saving', async () => {
    renderEditor();
    await openBoard();

    // Move page 3 (index 2) into section 1, then delete the emptied section 2.
    await userEvent.click(screen.getByRole('button', { name: /^Move page 3/i }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /^Section 1\b/ }));
    await userEvent.click(screen.getByRole('button', { name: /Delete section 2/i }));
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));

    await waitFor(() => {
      const call = graphql.mock.calls.find((c) => c[0].query === 'updateTestSetDocumentSections');
      expect(call).toBeDefined();
      // The whole point: pages 1,2,3 on screen are indices 0,1,2 on disk.
      expect(call![0].variables.input.sections).toEqual([{ sectionId: '1', documentClass: 'Invoice', pageIndices: [0, 1, 2] }]);
    });
  });

  it('warns which sections moved, and does not re-extract on its own', async () => {
    renderEditor();
    await openBoard();

    await userEvent.click(screen.getByRole('button', { name: /^Move page 3/i }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /^Section 1\b/ }));
    await userEvent.click(screen.getByRole('button', { name: /Delete section 2/i }));
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));

    // Names the section, because the reviewer needs to know which values to check.
    await waitFor(() => expect(screen.getByText(/Pages moved in section 1/i)).toBeInTheDocument());
    expect(screen.getByText(/your field values are untouched/i)).toBeInTheDocument();
    // Re-extraction is offered, never performed: doing it here would regenerate exactly
    // the values this feature exists to preserve.
    expect(graphql.mock.calls.some((c) => c[0].query === 'reextractTestSetDocument')).toBe(false);
  });

  it('states that field values and provenance survive, before anything is saved', async () => {
    renderEditor();
    await openBoard();

    // Matched on the consequence copy specifically: "Reviewed (human)" alone also hits
    // the provenance badge rendered above the editor.
    expect(screen.getByText(/provenance and the edit history are all kept/)).toBeInTheDocument();
    expect(screen.getByText(/ground truth for how the packet splits/)).toBeInTheDocument();
  });

  it('does not offer the board without a test set to save to', async () => {
    render(
      <GroundTruthVisualEditor
        bucket="test-set-bucket"
        inputKey="ts1/input/p.pdf"
        objectKey="p.pdf"
        sections={SECTIONS}
        isReadOnly={false}
      />,
    );

    // reextract and the grouping mutation are both keyed on the set; offering the button
    // would produce a save that cannot be addressed.
    await waitFor(() => expect(screen.getByRole('button', { name: /Edit page grouping/i })).toBeDisabled());
  });

  it('does not offer the board to someone who cannot change the class', async () => {
    render(
      <GroundTruthVisualEditor
        bucket="test-set-bucket"
        inputKey="ts1/input/p.pdf"
        objectKey="p.pdf"
        sections={SECTIONS}
        isReadOnly={false}
        testSetId="ts1"
        canChangeClass={false}
      />,
    );

    // Re-grouping is the same class of annotation authority as changing a class, and it
    // persists through the same kind of write.
    await waitFor(() => expect(screen.getByRole('button', { name: /Edit page grouping/i })).toBeDisabled());
  });
});

/**
 * Where the control lives, after review: beside the section pills rather than
 * among one section's extracted values.
 *
 * Re-grouping rewrites the document's structure, so sitting it next to the field data made
 * it read as part of that data. These are DOM-relationship and document-order assertions
 * on purpose — `getByRole('button', { name: /Edit page grouping/ })` passes wherever the
 * button happens to be, so a presence check could not have caught the old placement and
 * cannot protect the new one.
 */
describe('GroundTruthVisualEditor page grouping placement', () => {
  it('sits on the same row as the section pills', async () => {
    renderEditor();
    await waitFor(() => expect(screen.getByRole('button', { name: /Edit page grouping/i })).toBeEnabled());

    const button = screen.getByRole('button', { name: /Edit page grouping/i });
    const pill = screen.getByRole('button', { name: /^Section 2\b/ });

    expect(button.closest('div')).toContainElement(pill);
  });

  it('comes before the extracted values rather than inside them', async () => {
    renderEditor();
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());

    const button = screen.getByRole('button', { name: /Edit page grouping/i });
    // The read-only Pages field is where the button used to be, immediately after this
    // input — so in the old layout the input PRECEDED the button.
    const pagesField = screen.getByDisplayValue('1, 2');

    expect(button.compareDocumentPosition(pagesField) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('is still offered for a single-section document', async () => {
    // The regression the move could have introduced: the pills only render when there is
    // more than one section, and pairing the button with that condition would remove the
    // only route to splitting a single section into two.
    render(
      <GroundTruthVisualEditor
        bucket="test-set-bucket"
        inputKey="ts1/input/p.pdf"
        objectKey="p.pdf"
        sections={[SECTIONS[0]]}
        isReadOnly={false}
        testSetId="ts1"
      />,
    );

    await waitFor(() => expect(screen.getByRole('button', { name: /Edit page grouping/i })).toBeEnabled());
    // And with one section there are no pills to sit beside.
    expect(screen.queryByRole('button', { name: /^Section 2\b/ })).not.toBeInTheDocument();
  });

  it('leaves the pages field readable, without a control in it', async () => {
    renderEditor();
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());

    // Still informational — a reviewer looking at a section wants to know which pages it
    // covers — and it now says where the editing control went.
    expect(screen.getByText(/Use Edit page grouping, above/i)).toBeInTheDocument();
  });

  it('hides the control while the board is open, so it cannot be re-triggered', async () => {
    renderEditor();
    await openBoard();

    // The pills hide during regrouping; a lone button beside them would be a dead affordance
    // pointing at the thing already on screen.
    expect(screen.queryByRole('button', { name: /^Edit page grouping$/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Save page grouping/i })).toBeInTheDocument();
  });
});
