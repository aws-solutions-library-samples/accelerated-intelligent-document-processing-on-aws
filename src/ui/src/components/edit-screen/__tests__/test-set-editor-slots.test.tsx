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
  mayChangeClass = true,
}: {
  sections?: TestSetDocumentSectionRef[];
  mayChangeClass?: boolean;
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
    mayChangeClass,
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

/** The input of every reextractTestSetDocument call so far. */
const reextractInputs = () =>
  graphql.mock.calls.filter((c) => c[0].query === 'reextractTestSetDocument').map((c) => c[0].variables.input as Record<string, unknown>);

/**
 * Whether a Cloudscape button is actionable. A button with a `disabledReason` is marked
 * `aria-disabled` rather than `disabled`, so `toBeEnabled()` passes on it either way.
 */
const isActionable = (button: HTMLElement) => !(button as HTMLButtonElement).disabled && button.getAttribute('aria-disabled') !== 'true';

/** The explanation shown wherever a whole-document Re-extract would merge sections. */
const MERGE_EXPLANATION = /Changing the class for the whole document would merge all \d+ sections into one and discard their ground truth/;

describe('a class change on a document with more than one section (#1672, #1699)', () => {
  // A whole-document Re-extract forces one class onto every page as one section, so on a
  // packet it would merge every section (#1672). Such a document re-extracts the open
  // section only (#1699).
  const TWO_AFTER: TestSetDocumentSectionRef[] = [{ ...SECTIONS[0], documentClass: 'W2' }, SECTIONS[1]];
  const TWO_SAVED = [
    { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
    { sectionId: '2', documentClass: 'W2', pageIndices: [2] },
  ];

  it('offers Re-extract for the open section only, says why, and runs it only on request', async () => {
    answer(TWO_SAVED);
    render(<Harness afterChange={TWO_AFTER} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    expect(await screen.findByText('Fields still reflect the previous class')).toBeInTheDocument();
    expect(screen.getByText(MERGE_EXPLANATION)).toHaveTextContent('merge all 2 sections into one');
    expect(screen.getByText(MERGE_EXPLANATION)).toHaveTextContent('Re-extract here works on this section only');
    expect(screen.getByText(/the other 1 section keeps its pages, class and fields/)).toBeInTheDocument();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(isActionable(button)).toBe(true));
    expect(reextractInputs()).toEqual([]);
    await userEvent.click(button);
    await waitFor(() => expect(reextractInputs()).toHaveLength(1));
    // The class is the open section's, as the editor holds it (this harness re-reads the same baseline).
    expect(reextractInputs()[0]).toMatchObject({ testSetId: 'ts1', objectKey: 'p.pdf', documentClass: 'Invoice', sectionId: '1' });
  });

  it('re-extracts the section that is open, not the first one', async () => {
    // Section 2 (page 3) changes class on the board and is then opened in the editor.
    answer([
      { sectionId: '1', documentClass: 'Invoice', pageIndices: [0, 1] },
      { sectionId: '2', documentClass: 'Invoice', pageIndices: [2] },
    ]);
    render(<Harness afterChange={[SECTIONS[0], { ...SECTIONS[1], documentClass: 'Invoice' }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    const second = createWrapper(document.body).findModal()!.findContent().findAllSelects()[1];
    act(() => second.openDropdown());
    act(() => second.selectOptionByValue('Invoice'));
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const sectionPicker = createWrapper(document.body)
      .findAllButtonDropdowns()
      .find((d) => (d.findNativeButton().getElement().getAttribute('aria-label') ?? '').startsWith('Section:'))!;
    act(() => sectionPicker.openDropdown());
    act(() => sectionPicker.findItems()[1].click());
    const button = await screen.findByRole('button', { name: 'Re-extract section 2' });
    await waitFor(() => expect(isActionable(button)).toBe(true));
    await userEvent.click(button);
    await waitFor(() => expect(reextractInputs()).toHaveLength(1));
    expect(reextractInputs()[0]).toMatchObject({ sectionId: '2' });
  });

  it('does not let a caller who may not change the class re-extract a section', async () => {
    answer(TWO_SAVED);
    const { rerender } = render(<Harness afterChange={TWO_AFTER} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(isActionable(button)).toBe(true));
    // The same page, for a caller whose role may not change this document's class.
    rerender(<Harness afterChange={TWO_AFTER} mayChangeClass={false} />);
    expect(isActionable(screen.getByRole('button', { name: 'Re-extract section 1' }))).toBe(false);
  });

  it('says the section, not the document, could not be re-extracted', async () => {
    answer(TWO_SAVED);
    const answered = graphql.getMockImplementation()!;
    graphql.mockImplementation((args: { query: string }) =>
      args.query === 'reextractTestSetDocument' ? Promise.reject(new Error('the run failed')) : answered(args),
    );
    render(<Harness afterChange={TWO_AFTER} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(isActionable(button)).toBe(true));
    await userEvent.click(button);
    expect(await screen.findByText(/Could not re-extract this section: /)).toBeInTheDocument();
  });

  it('shows the reason a failed section re-extract gives', async () => {
    answer(TWO_SAVED);
    const answered = graphql.getMockImplementation()!;
    graphql.mockImplementation((args: { query: string }) => {
      if (args.query === 'reextractTestSetDocument') {
        return Promise.resolve({ data: { reextractTestSetDocument: { jobId: 'j1', status: 'RUNNING' } } });
      }
      if (args.query === 'getDraftLabelJob') {
        return Promise.resolve({
          data: { getDraftLabelJob: { jobId: 'j1', status: 'FAILED', error: 'Section 1 was changed while it was being re-extracted.' } },
        });
      }
      return answered(args);
    });
    render(<Harness afterChange={TWO_AFTER} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(isActionable(button)).toBe(true));
    await userEvent.click(button);
    expect(
      await screen.findByText(/Could not re-extract this section: Section 1 was changed while it was being re-extracted\./, undefined, {
        timeout: 8000,
      }),
    ).toBeInTheDocument();
  }, 15000);

  it('says an authored section keeps its values when re-extracted', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: { total: '1' } });
    answer(TWO_SAVED);
    render(<Harness afterChange={TWO_AFTER} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    expect(await screen.findByText(/this section has authored ground truth, so its field values are kept/)).toBeInTheDocument();
  });

  it('does not show the merge explanation on a single-section document', async () => {
    answer([{ sectionId: '1', documentClass: 'W2', pageIndices: [0, 1, 2] }]);
    render(<Harness afterChange={[{ ...SECTIONS[0], documentClass: 'W2', pageIndices: [0, 1, 2] }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
    await mergeAndSave();
    expect(await screen.findByRole('button', { name: /^Re-extract$/ })).toBeInTheDocument();
    expect(screen.queryByText(MERGE_EXPLANATION)).not.toBeInTheDocument();
  });

  // One section over all three pages: the whole-document form.
  const ONE_ALL: TestSetDocumentSectionRef[] = [{ ...SECTIONS[0], pageIndices: [0, 1, 2] }];

  it('on a single-section document, re-extracts the whole document, without a sectionId', async () => {
    answer([{ sectionId: '1', documentClass: 'W2', pageIndices: [0, 1, 2] }]);
    render(<Harness sections={ONE_ALL} afterChange={[{ ...ONE_ALL[0], documentClass: 'W2' }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: /^Re-extract$/ });
    expect(reextractInputs()).toEqual([]);
    await userEvent.click(button);
    await waitFor(() => expect(reextractInputs()).toHaveLength(1));
    expect(reextractInputs()[0]).toMatchObject({ testSetId: 'ts1', objectKey: 'p.pdf', documentClass: 'Invoice' });
    expect(reextractInputs()[0]).not.toHaveProperty('sectionId');
  });

  it('counts the grouping just saved before the refs catch up, and waits for them before re-extracting a section', async () => {
    // The save wrote two sections; the page has not re-read its refs yet (no afterChange).
    // Sections are renumbered on save, so the open section's id is not yet trustworthy.
    answer([
      { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
      { sectionId: '2', documentClass: 'Invoice', pageIndices: [2] },
    ]);
    render(<Harness sections={ONE_ALL} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    expect(await screen.findByText('Fields still reflect the previous class')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Re-extract$/ })).not.toBeInTheDocument();
    expect(isActionable(screen.getByRole('button', { name: 'Re-extract section 1' }))).toBe(false);
    expect(screen.getByText(MERGE_EXPLANATION)).toBeInTheDocument();
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

  it('enables the per-section Re-extract once the refs of a split arrive', async () => {
    answer([
      { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
      { sectionId: '2', documentClass: 'Invoice', pageIndices: [2] },
    ]);
    const split: TestSetDocumentSectionRef[] = [
      { ...SECTIONS[0], documentClass: 'W2', pageIndices: [0, 1] },
      { ...SECTIONS[1], documentClass: 'Invoice', pageIndices: [2] },
    ];
    render(<Harness sections={ONE_ALL} afterChange={split} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(isActionable(button)).toBe(true));
  });

  it('points an empty classed section at the per-section Re-extract on a multi-section document', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: {} });
    render(<Harness />);
    expect(await screen.findByText(/No field values for this section/i)).toBeInTheDocument();
    expect(screen.getByText(/To have the model populate it/)).toHaveTextContent(
      're-extracts this section only and leaves the others as they are',
    );
  });

  it('points an empty classed section at Re-extract on a single-section document', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: {} });
    render(<Harness sections={ONE_ALL} />);
    expect(await screen.findByText(/No field values for this section/i)).toBeInTheDocument();
    expect(screen.getByText(/To have the model populate it/)).toHaveTextContent('choose Re-extract');
    expect(screen.getByText(/To have the model populate it/)).not.toHaveTextContent('this section only');
  });

  it('says in the moved-pages warning that Re-extract works on one section', async () => {
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
    expect(body).toMatch(/on this document it re-extracts that one section\)\.$/);
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

describe('the note a finished re-extract leaves (#1728)', () => {
  const TWO_AFTER: TestSetDocumentSectionRef[] = [{ ...SECTIONS[0], documentClass: 'W2' }, SECTIONS[1]];
  const TWO_SAVED = [
    { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
    { sectionId: '2', documentClass: 'W2', pageIndices: [2] },
  ];
  const fetchCalls = () => (global.fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length;

  /** Reclassify section 1, re-extract it, and wait until the editor has re-read it. */
  const reextractSectionOne = async () => {
    answer(TWO_SAVED);
    render(<Harness afterChange={TWO_AFTER} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(isActionable(button)).toBe(true));
    const before = fetchCalls();
    await userEvent.click(button);
    // The re-extract bumps the reload token, so the open section is read again.
    await waitFor(() => expect(fetchCalls()).toBeGreaterThan(before));
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
  };

  it('is still showing once the re-extracted section has been read again', async () => {
    await reextractSectionOne();
    expect(screen.getByText('Re-extracted section 1 as Invoice. The other sections were not changed.')).toBeInTheDocument();
  });

  it('goes once another section is opened', async () => {
    await reextractSectionOne();
    const sectionPicker = createWrapper(document.body)
      .findAllButtonDropdowns()
      .find((d) => (d.findNativeButton().getElement().getAttribute('aria-label') ?? '').startsWith('Section:'))!;
    act(() => sectionPicker.openDropdown());
    act(() => sectionPicker.findItems()[1].click());
    await waitFor(() => expect(screen.queryByText(/^Re-extracted section 1 as/)).not.toBeInTheDocument());
  });

  it('on authored ground truth, says the class was corrected and the values kept, not that it re-extracted', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: { total: '1' } });
    await reextractSectionOne();
    const note = screen.getByText(/^Class corrected to Invoice/);
    expect(note).toHaveTextContent('authored ground truth');
    expect(note).toHaveTextContent('field values were kept');
    expect(note).toHaveTextContent('JSON Editor');
    expect(screen.queryByText(/^Re-extracted/)).not.toBeInTheDocument();
  });

  it('on a single-section document of authored ground truth, says the same of the document', async () => {
    withBaseline({ document_class: { type: 'Invoice' }, split_document: { page_indices: [0, 1] }, inference_result: { total: '1' } });
    const ONE_ALL: TestSetDocumentSectionRef[] = [{ ...SECTIONS[0], pageIndices: [0, 1, 2] }];
    answer([{ sectionId: '1', documentClass: 'W2', pageIndices: [0, 1, 2] }]);
    render(<Harness sections={ONE_ALL} afterChange={[{ ...ONE_ALL[0], documentClass: 'W2' }]} />);
    await waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());
    await reclassifyFirstAndSave();
    await userEvent.click(await screen.findByRole('button', { name: /^Re-extract$/ }));
    const note = await screen.findByText(/^Class corrected to Invoice/);
    expect(note).toHaveTextContent('This document has authored ground truth');
  });
});
