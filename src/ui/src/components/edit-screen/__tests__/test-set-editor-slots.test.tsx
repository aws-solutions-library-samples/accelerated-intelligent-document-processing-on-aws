// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Test Studio slots over the real widget SectionEditor and the page's board hook,
 * as the edit page wires them. What has to hold: the board works in 1-based page ids
 * and the baseline stores 0-based `page_indices`, converted on the way in and on the
 * way out (getting it wrong would shift every section by one page and raise nothing);
 * a regroup names the sections whose pages moved and re-extracts nothing on its own;
 * a section whose class Edit Page Grouping changed offers Re-extract, which runs only when
 * asked; there is no separate class dropdown; and a section with no field values says
 * which of three causes applies and what to do about it.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';
import React, { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
const onChanged = vi.fn();
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
  default: () => ({ mergedConfig: { classes: [{ $id: 'Invoice' }, { $id: 'W2' }] }, loading: false, error: null }),
}));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => ({ allowedConfigVersions: null, loading: false }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ user: { username: 'tester' }, setSuccessMessage: vi.fn() }) }));

import SectionEditor from '../SectionEditor';
import { useTestSetEditorSlots } from '../test-set-editor-slots';
import type { TestSetDocumentSectionRef } from '../test-set-editor-slots';
import usePageClassBoard from '../use-page-class-board';

/** The open section's baseline: pages 1-2 in the reviewer's terms, [0,1] on disk. */
const BASELINE = {
  document_class: { type: 'Invoice' },
  split_document: { page_indices: [0, 1] },
  inference_result: { total: '10.00' },
  labelSource: 'reviewed-human',
};

const SECTIONS: TestSetDocumentSectionRef[] = [
  { sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: 'Invoice', pageIndices: [0, 1] },
  { sectionId: '2', baselineKey: 'ts1/baseline/p.pdf/sections/2/result.json', documentClass: 'W2', pageIndices: [2] },
];

/**
 * The edit page's wiring of the slots, without the rest of the page. `afterChange` is the
 * section refs the page re-reads once the slots report a change, as the real page does:
 * without it the refs stay as they were, which no real page does after a regroup.
 */
const Harness = ({
  sections = SECTIONS,
  afterChange,
  afterChanges,
}: {
  sections?: TestSetDocumentSectionRef[];
  afterChange?: TestSetDocumentSectionRef[];
  /** One set of refs per change, in order, for a test that saves more than once. */
  afterChanges?: TestSetDocumentSectionRef[][];
}): React.JSX.Element => {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [refs, setRefs] = useState(sections);
  const [changes, setChanges] = useState(0);
  const slots = useTestSetEditorSlots({
    bucket: 'test-set-bucket',
    inputKey: 'ts1/input/p.pdf',
    objectKey: 'p.pdf',
    testSetId: 'ts1',
    sections: refs,
    mayChangeClass: true,
    openSectionId: openId,
    openData: data,
    onChanged: () => {
      onChanged();
      const next = afterChanges ? afterChanges[changes] : afterChange;
      setChanges((n) => n + 1);
      if (next) setRefs(next);
    },
  });
  const board = usePageClassBoard(slots.board, openId, data);
  return (
    <>
      {/* The edit page's More Actions › Edit Page Grouping, offered when the board has something to show. */}
      <button type="button" onClick={board.open} disabled={!slots.canRegroup}>
        Edit Page Grouping
      </button>
      {board.dialog}
      <SectionEditor
        objectKey="p.pdf"
        sections={slots.reviewSections}
        pages={slots.pages}
        isReadOnly={false}
        loadSection={slots.loadSection}
        onSave={vi.fn()}
        onLoaded={slots.onLoaded}
        reloadToken={slots.reloadToken}
        headerExtras={slots.headerExtras}
        classSlot={slots.classSlot}
        emptyFieldsSlot={slots.emptyFieldsSlot}
        pagesNotice={slots.pagesNotice}
        onDataChange={setData}
        onSelectedSectionChange={setOpenId}
      />
    </>
  );
};

const answer = (grouped: { sectionId: string; documentClass: string; pageIndices: number[] }[]) =>
  graphql.mockImplementation((args: { query: string }) => {
    if (args.query === 'getFilePresignedUrl') {
      return Promise.resolve({ data: { getFilePresignedUrl: { presignedUrl: 'https://example.test/signed' } } });
    }
    if (args.query === 'updateTestSetDocumentSections') {
      return Promise.resolve({ data: { updateTestSetDocumentSections: { testSetId: 'ts1', objectKey: 'p.pdf', sections: grouped } } });
    }
    if (args.query === 'reextractTestSetDocument') {
      return Promise.resolve({ data: { reextractTestSetDocument: { jobId: 'j1', status: 'SUCCEEDED' } } });
    }
    return Promise.resolve({ data: {} });
  });

/** Move page 3 into section 1, delete the emptied section 2, and save. */
const mergeAndSave = async () => {
  await userEvent.click(screen.getByRole('button', { name: /^Move page 3/i }));
  await userEvent.click(await screen.findByRole('menuitem', { name: /^Section 1\b/ }));
  await userEvent.click(screen.getByRole('button', { name: /Delete section 2/i }));
  await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));
};

beforeEach(() => {
  graphql.mockReset();
  onChanged.mockReset();
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve(JSON.stringify(BASELINE)) })) as never;
  answer([{ sectionId: '1', documentClass: 'Invoice', pageIndices: [0, 1, 2] }]);
});

describe('the Test Studio slots', () => {
  it('show 0-based page indices as 1-based pages, on the board and in the pages field, and save them 0-based', async () => {
    render(<Harness />);
    // [0, 1] on disk is "1, 2" on screen.
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    // Every section, from the payload; page 3 is index 2 on disk.
    expect(screen.getByText('Section 2')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Select page 3/i })).toBeInTheDocument();
    await mergeAndSave();
    await waitFor(() => {
      const call = graphql.mock.calls.find((c) => c[0].query === 'updateTestSetDocumentSections');
      expect(call![0].variables.input.sections).toEqual([{ sectionId: '1', documentClass: 'Invoice', pageIndices: [0, 1, 2] }]);
    });
    // The caller re-reads its refs; the sections that moved are named.
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(await screen.findByText(/Pages moved in section 1/i)).toBeInTheDocument();
  });

  it('offer Re-extract after a class change, and run it only on request', async () => {
    // The saved grouping comes back with section 1 (first page 1, formerly Invoice) as W2.
    answer([{ sectionId: '1', documentClass: 'W2', pageIndices: [0, 1, 2] }]);
    render(<Harness afterChange={[{ ...SECTIONS[0], documentClass: 'W2', pageIndices: [0, 1, 2] }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    await mergeAndSave();
    expect(await screen.findByText('Fields still reflect the previous class')).toBeInTheDocument();
    expect(screen.getByText(/These fields were extracted as/)).toHaveTextContent('Invoice');
    expect(graphql.mock.calls.some((c) => c[0].query === 'reextractTestSetDocument')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: /^Re-extract$/ }));
    await waitFor(() => expect(graphql.mock.calls.some((c) => c[0].query === 'reextractTestSetDocument')).toBe(true));
  });
});

/** Change section 1's class to W2 on the board, keeping both sections, and save. */
const reclassifyFirstAndSave = async () => {
  await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
  const select = createWrapper(document.body).findModal()!.findContent().findSelect()!;
  act(() => select.openDropdown());
  act(() => select.selectOptionByValue('W2'));
  await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));
};

describe('a class change on a document with more than one section (#1672)', () => {
  // Re-extract re-runs the whole document as one section of the forced class, so offering
  // it on a section of a packet rewrote every section's class and collapsed the grouping.
  const TWO_AFTER: TestSetDocumentSectionRef[] = [{ ...SECTIONS[0], documentClass: 'W2' }, SECTIONS[1]];

  it('says the fields are stale but offers no Re-extract, and never calls it', async () => {
    answer([
      { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
      { sectionId: '2', documentClass: 'W2', pageIndices: [2] },
    ]);
    render(<Harness afterChange={TWO_AFTER} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    expect(await screen.findByText('Fields still reflect the previous class')).toBeInTheDocument();
    expect(screen.getByText(/Re-extract is not offered on a document with more than one section/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Re-extract$/ })).not.toBeInTheDocument();
    expect(graphql.mock.calls.some((c) => c[0].query === 'reextractTestSetDocument')).toBe(false);
  });

  it('still offers Re-extract once the document is down to one section', async () => {
    answer([{ sectionId: '1', documentClass: 'W2', pageIndices: [0, 1, 2] }]);
    render(<Harness afterChange={[{ ...SECTIONS[0], documentClass: 'W2', pageIndices: [0, 1, 2] }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    await mergeAndSave();
    expect(await screen.findByRole('button', { name: /^Re-extract$/ })).toBeInTheDocument();
    expect(screen.queryByText(/Re-extract is not offered/)).not.toBeInTheDocument();
  });

  // One section over all three pages: the only shape Re-extract is offered on.
  const ONE_ALL: TestSetDocumentSectionRef[] = [{ ...SECTIONS[0], pageIndices: [0, 1, 2] }];

  it('on a single-section document, offers Re-extract after a class change and runs it only on request', async () => {
    answer([{ sectionId: '1', documentClass: 'W2', pageIndices: [0, 1, 2] }]);
    render(<Harness sections={ONE_ALL} afterChange={[{ ...ONE_ALL[0], documentClass: 'W2' }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: /^Re-extract$/ });
    expect(graphql.mock.calls.some((c) => c[0].query === 'reextractTestSetDocument')).toBe(false);
    await userEvent.click(button);
    await waitFor(() => expect(graphql.mock.calls.some((c) => c[0].query === 'reextractTestSetDocument')).toBe(true));
  });

  it('counts the grouping just saved before the refs catch up, so a split hides Re-extract at once', async () => {
    // The save wrote two sections; the page has not re-read its refs yet (no afterChange).
    answer([
      { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
      { sectionId: '2', documentClass: 'Invoice', pageIndices: [2] },
    ]);
    render(<Harness sections={ONE_ALL} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    expect(await screen.findByText('Fields still reflect the previous class')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Re-extract$/ })).not.toBeInTheDocument();
  });

  it('drops the saved count once the page re-reads its sections, and trusts the refs', async () => {
    // The save wrote two sections, but the re-read finds one (another session merged them).
    answer([
      { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
      { sectionId: '2', documentClass: 'Invoice', pageIndices: [2] },
    ]);
    render(<Harness sections={ONE_ALL} afterChange={[{ ...ONE_ALL[0], documentClass: 'W2' }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    expect(await screen.findByRole('button', { name: /^Re-extract$/ })).toBeInTheDocument();
  });

  it('offers Re-extract again after a split is merged back to one section', async () => {
    // An empty classed section, so the empty-section notice shows whether Re-extract is offered.
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: {} });
    const split: TestSetDocumentSectionRef[] = [
      { ...SECTIONS[0], documentClass: 'W2', pageIndices: [0, 1] },
      { ...SECTIONS[1], documentClass: 'Invoice', pageIndices: [2] },
    ];
    render(<Harness sections={ONE_ALL} afterChanges={[split, [{ ...ONE_ALL[0], documentClass: 'W2' }]]} />);
    expect(await screen.findByText(/To have the model populate it/)).toBeInTheDocument();
    answer(split.map((sec) => ({ sectionId: sec.sectionId, documentClass: sec.documentClass!, pageIndices: sec.pageIndices! })));
    await reclassifyFirstAndSave();
    expect((await screen.findAllByText(/Re-extract is not offered on a document with more than one section/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/To have the model populate it/)).not.toBeInTheDocument();
    answer([{ sectionId: '1', documentClass: 'W2', pageIndices: [0, 1, 2] }]);
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    await mergeAndSave();
    expect(await screen.findByText(/To have the model populate it/)).toBeInTheDocument();
  });

  it('does not point an empty classed section at Re-extract on a multi-section document', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: {} });
    render(<Harness />);
    expect(await screen.findByText(/No field values for this section/i)).toBeInTheDocument();
    expect(screen.queryByText(/To have the model populate it/)).not.toBeInTheDocument();
    expect(screen.getByText(/Re-extract is not offered on a document with more than one section/)).toBeInTheDocument();
  });

  it('does point an empty classed section at Re-extract on a single-section document', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: {} });
    render(<Harness sections={ONE_ALL} />);
    expect(await screen.findByText(/No field values for this section/i)).toBeInTheDocument();
    expect(screen.getByText(/To have the model populate it/)).toHaveTextContent('choose Re-extract');
  });

  it('does not suggest Re-extract in the moved-pages warning', async () => {
    answer([
      { sectionId: '1', documentClass: 'Invoice', pageIndices: [0] },
      { sectionId: '2', documentClass: 'W2', pageIndices: [1, 2] },
    ]);
    render(
      <Harness
        afterChange={[
          { ...SECTIONS[0], pageIndices: [0] },
          { ...SECTIONS[1], pageIndices: [1, 2] },
        ]}
      />,
    );
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    await userEvent.click(screen.getByRole('button', { name: /^Move page 2/i }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /^Section 2\b/ }));
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));
    expect(await screen.findByText(/Pages moved in section/i)).toBeInTheDocument();
    const body = screen.getByText(/may no longer match/).textContent ?? '';
    expect(body).toMatch(/Check them\.$/);
    expect(body).not.toMatch(/Re-extract/);
  });
});

describe('regrouping in the Test Studio slots', () => {
  it('names the sections whose pages moved, and does not re-extract on its own', async () => {
    render(<Harness />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    await mergeAndSave();
    expect(await screen.findByText(/Pages moved in section 1/i)).toBeInTheDocument();
    expect(screen.getByText(/your field values are untouched/i)).toBeInTheDocument();
    expect(graphql.mock.calls.some((c) => c[0].query === 'reextractTestSetDocument')).toBe(false);
  });

  it('has no Class label dropdown: a section is reclassified in Edit Page Grouping', async () => {
    render(<Harness />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    expect(screen.queryByText('Class label')).not.toBeInTheDocument();
  });

  it('states that field values and provenance survive, before anything is saved', async () => {
    render(<Harness />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    expect(screen.getByText(/provenance and the edit history are all kept/)).toBeInTheDocument();
    expect(screen.getByText(/ground truth for how the packet splits/)).toBeInTheDocument();
  });
});

const ONE_SECTION: TestSetDocumentSectionRef[] = [SECTIONS[0]];
const withBaseline = (baseline: Record<string, unknown>) => {
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve(JSON.stringify(baseline)) })) as never;
};

describe('a Test Studio section with no field values', () => {
  it('explains an empty result instead of rendering a bare heading', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0] }, inference_result: {} });
    render(<Harness sections={ONE_SECTION} />);
    expect(await screen.findByText(/No field values for this section/i)).toBeInTheDocument();
    expect(await screen.findByText(/extraction ran but produced no fields/i)).toBeInTheDocument();
    expect(screen.queryByText('Document Data')).not.toBeInTheDocument();
  });

  it('names re-grouping as the other way a classified section gets here', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0] }, inference_result: {} });
    render(<Harness sections={ONE_SECTION} />);
    expect(await screen.findByText(/re-grouped/i)).toBeInTheDocument();
  });

  it('blames the missing class when there is one, and says re-extracting will not help', async () => {
    withBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    render(<Harness sections={ONE_SECTION} />);
    expect(await screen.findByText(/no document class/i)).toBeInTheDocument();
    expect(screen.getByText(/no schema to extract against/i)).toBeInTheDocument();
    expect(screen.getByText(/re-extracting before that will not produce anything/i)).toBeInTheDocument();
    expect(screen.getByText(/can merge them/i)).toBeInTheDocument();
  });

  it('points an unclassified section at Edit Page Grouping, which is offered', async () => {
    withBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    render(<Harness sections={ONE_SECTION} />);
    expect(await screen.findByText(/Set its class in Edit Page Grouping first/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit Page Grouping' })).toBeEnabled();
    expect(screen.queryByText('Class label')).not.toBeInTheDocument();
  });

  it('still distinguishes a missing result from an empty one', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0] } });
    render(<Harness sections={ONE_SECTION} />);
    expect(await screen.findByText(/no inference_result at all/i)).toBeInTheDocument();
  });

  it('points at the other sections when the document has more than one', async () => {
    withBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    render(<Harness />);
    expect(await screen.findByText(/Other sections of this document may still have values/i)).toBeInTheDocument();
  });

  it('says nothing about other sections on a single-section document', async () => {
    withBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    render(<Harness sections={ONE_SECTION} />);
    await waitFor(() => expect(screen.getByText(/No field values for this section/i)).toBeInTheDocument());
    expect(screen.queryByText(/Other sections of this document/i)).not.toBeInTheDocument();
  });

  it('marks an unclassified section in the Section dropdown, and names every class from the payload', async () => {
    withBaseline({ split_document: { page_indices: [0] }, inference_result: {} });
    render(
      <Harness
        sections={[
          { sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: null, pageIndices: [0] },
          { sectionId: '2', baselineKey: 'ts1/baseline/p.pdf/sections/2/result.json', documentClass: 'Invoice', pageIndices: [1] },
        ]}
      />,
    );
    await waitFor(() => expect(screen.getByText(/No field values for this section/i)).toBeInTheDocument());
    const select = createWrapper(document.body)
      .findAllButtonDropdowns()
      .find((d) => (d.findNativeButton().getElement().getAttribute('aria-label') ?? '').startsWith('Section:'))!;
    act(() => select.openDropdown());
    const labels = select.findItems().map((o) => o.getElement().textContent);
    expect(labels).toEqual(['Section 1 (no class)', 'Section 2 (Invoice)']);
  });

  it('renders the fields normally when there are any', async () => {
    withBaseline({ split_document: { page_indices: [0] }, inference_result: { total: '10.00' } });
    render(<Harness sections={ONE_SECTION} />);
    await waitFor(() => expect(screen.getByDisplayValue('10.00')).toBeInTheDocument());
    expect(screen.queryByText(/No field values for this section/i)).not.toBeInTheDocument();
  });
});
