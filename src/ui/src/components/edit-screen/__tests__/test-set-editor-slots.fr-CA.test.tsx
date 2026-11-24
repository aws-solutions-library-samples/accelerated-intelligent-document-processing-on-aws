// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The section slots of a test-set item's edit page under `fr-CA`, over the real section editor
 * and Edit Page Grouping board, as the page wires them: with a fixture catalog the warning after a
 * saved grouping moved pages names the section in French, with Re-extract in bold inside its
 * sentence; the notice that a section's fields still reflect its previous class reads French
 * around the two class names, and its button is Re-extract with the French accessible name of the
 * section; a finished or failed re-extract leaves a French note. The re-extract starts and polls
 * once per click, and the open section is read once: neither runs again on a switch to English,
 * where the note reads English. Under `en` the same renders read today's English.
 */
import React, { useState } from 'react';
import { act, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  /** The open section's baseline, as the editor reads it. */
  baseline: {} as Record<string, unknown>,
  pages: {
    pages: [
      { Id: '1', ImageUri: 'blob:1' },
      { Id: '2', ImageUri: 'blob:2' },
      { Id: '3', ImageUri: 'blob:3' },
    ],
    isLoading: false,
    error: null,
    previewUnavailable: false,
  },
  configuration: { mergedConfig: { classes: [{ $id: 'Invoice' }, { $id: 'W2' }] }, loading: false, error: null },
  versions: { versions: [] },
  role: { allowedConfigVersions: null, loading: false },
  app: { user: { username: 'annotator' }, setSuccessMessage: vi.fn() },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  getFilePresignedUrl: 'getFilePresignedUrl',
  uploadDocument: 'uploadDocument',
  reextractTestSetDocument: 'reextractTestSetDocument',
  getDraftLabelJob: 'getDraftLabelJob',
  updateTestSetDocumentSections: 'updateTestSetDocumentSections',
}));
vi.mock('../../../hooks/use-test-doc-pages', () => ({ default: () => state.pages }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => state.configuration }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => state.versions }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));

import SectionEditor from '../SectionEditor';
import { useTestSetEditorSlots } from '../test-set-editor-slots';
import type { TestSetDocumentSectionRef } from '../test-set-editor-slots';
import usePageClassBoard from '../use-page-class-board';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'editScreen.testSetSlots.pagesInSection': 'Pages de cette section',
  'editScreen.testSetSlots.pagesMovedHeader':
    '{count, plural, one {Pages déplacées dans la section {ids}} other {Pages déplacées dans les sections {ids}}}',
  'editScreen.testSetSlots.pagesMovedBodyPerSection':
    'Le regroupement est enregistré et vos valeurs de champs sont intactes, mais ces sections ont été extraites d’un autre ensemble de pages : leurs valeurs pourraient ne plus correspondre. Vérifiez-les, changez la classe de la section et utilisez <b>Réextraire</b> si vous préférez que le modèle les refasse (sur ce document, cela réextrait cette seule section).',
  'editScreen.testSetSlots.previousClassHeader': 'Les champs reflètent encore la classe précédente',
  'editScreen.testSetSlots.previousClassSection':
    'Ces champs ont été extraits comme <b>{previousClass}</b>. Réextraire remplace les champs de cette section par ceux que produit le schéma <b>{documentClass}</b>; {others, plural, one {# autre section garde ses pages, sa classe et ses champs} other {# autres sections gardent leurs pages, leur classe et leurs champs}}.',
  'editScreen.testSetSlots.sectionTailReviewed':
    'Cette section était déjà marquée comme révisée; la réextraire efface ses valeurs confirmées.',
  'editScreen.testSetSlots.mergeWarning':
    'Changer la classe du document entier fusionnerait ses {count} sections en une seule et effacerait leur vérité terrain; Réextraire ne vise donc ici que cette section.',
  'editScreen.testSetSlots.reextract': 'Réextraire',
  'editScreen.testSetSlots.reextractSectionAria': 'Réextraire la section {sectionId}',
  'editScreen.testSetSlots.reextractedSection':
    'Section {sectionId} réextraite comme {documentClass}. Les autres sections n’ont pas changé.',
  'editScreen.testSetSlots.reextractFailedSection': 'Impossible de réextraire cette section : {error}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
type IntlOptions = Parameters<typeof renderWithIntl>[1];

/** Section 1 covers pages 1-2 ([0, 1] on disk), section 2 page 3. */
const SECTIONS: TestSetDocumentSectionRef[] = [
  { sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: 'Invoice', pageIndices: [0, 1] },
  { sectionId: '2', baselineKey: 'ts1/baseline/p.pdf/sections/2/result.json', documentClass: 'W2', pageIndices: [2] },
];
/** The refs the page re-reads after section 1 became W2. */
const RECLASSIFIED: TestSetDocumentSectionRef[] = [{ ...SECTIONS[0], documentClass: 'W2' }, SECTIONS[1]];
const BASELINE = {
  document_class: { type: 'Invoice' },
  split_document: { page_indices: [0, 1] },
  inference_result: { total: '10.00' },
  labelSource: 'reviewed-human',
};

/** The edit page's wiring of the slots, without the rest of the page; the refs are re-read once the slots report a change. */
const Harness = ({ afterChange }: { afterChange?: TestSetDocumentSectionRef[] }): React.JSX.Element => {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [refs, setRefs] = useState(SECTIONS);
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
      if (afterChange) setRefs(afterChange);
    },
  });
  const board = usePageClassBoard(slots.board, openId, data);
  return (
    <>
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

/** How the server answers: the grouping a save writes, and how a re-extract runs. */
const server: { written: unknown[]; reextract: 'succeeded' | 'polled' | 'failed' } = { written: [], reextract: 'succeeded' };

const callsOf = (query: string) => state.graphql.mock.calls.filter((c) => (c[0] as { query: string }).query === query).length;
const sectionReads = () => (global.fetch as unknown as { mock: { calls: unknown[] } }).mock.calls.length;

const renderSlots = (options: IntlOptions = {}, afterChange?: TestSetDocumentSectionRef[]) =>
  renderWithIntl(<Harness afterChange={afterChange} />, options);

/** The open section's pages field shows its pages, so the section is read. */
const opened = () => waitFor(() => expect(screen.getByDisplayValue('1, 2')).toBeInTheDocument());

/** Move page 3 into section 1, delete the emptied section 2, and save: section 1's pages moved. */
const mergeAndSave = async () => {
  server.written = [{ sectionId: '1', documentClass: 'Invoice', pageIndices: [0, 1, 2] }];
  await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
  await userEvent.click(screen.getByRole('button', { name: /^Move page 3/i }));
  await userEvent.click(await screen.findByRole('menuitem', { name: /^Section 1\b/ }));
  await userEvent.click(screen.getByRole('button', { name: /Delete section 2/i }));
  await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));
};

/** Change section 1's class to W2 on the board, keeping both sections, and save. */
const reclassifyFirstAndSave = async () => {
  server.written = [
    { sectionId: '1', documentClass: 'W2', pageIndices: [0, 1] },
    { sectionId: '2', documentClass: 'W2', pageIndices: [2] },
  ];
  // The section is read again after the save, as W2 now.
  state.baseline = { ...BASELINE, document_class: { type: 'W2' } };
  await userEvent.click(screen.getByRole('button', { name: 'Edit Page Grouping' }));
  const select = createWrapper(document.body).findModal()!.findContent().findSelect()!;
  act(() => select.openDropdown());
  act(() => select.selectOptionByValue('W2'));
  await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));
};

/** The alert whose heading reads `header`. */
const alertHeaded = (header: string) =>
  createWrapper(document.body)
    .findAllAlerts()
    .find((a) => a.findHeader()?.getElement().textContent === header) ?? null;

/** The alert holding `text`. */
const alertHolding = (text: string) =>
  createWrapper(document.body)
    .findAllAlerts()
    .find((a) => a.findContent().getElement().textContent === text) ?? null;

beforeEach(() => {
  document.body.innerHTML = '';
  state.baseline = BASELINE;
  server.written = [];
  server.reextract = 'succeeded';
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve(JSON.stringify(state.baseline)) })) as never;
  state.graphql.mockReset().mockImplementation(async ({ query }: { query: string }) => {
    if (query === 'getFilePresignedUrl') return { data: { getFilePresignedUrl: { presignedUrl: 'https://example.test/signed' } } };
    if (query === 'updateTestSetDocumentSections') {
      return { data: { updateTestSetDocumentSections: { testSetId: 'ts1', objectKey: 'p.pdf', sections: server.written } } };
    }
    if (query === 'reextractTestSetDocument') {
      if (server.reextract === 'failed') throw new Error('the run failed');
      return { data: { reextractTestSetDocument: { jobId: 'j1', status: server.reextract === 'polled' ? 'RUNNING' : 'SUCCEEDED' } } };
    }
    if (query === 'getDraftLabelJob') return { data: { getDraftLabelJob: { jobId: 'j1', status: 'SUCCEEDED' } } };
    return { data: {} };
  });
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the test-set editor’s section slots under fr-CA', () => {
  it('reads the open section once when the catalog arrives', async () => {
    renderSlots(FR_CA);
    expect(await screen.findByText('Pages de cette section')).toBeInTheDocument();
    await opened();
    expect(sectionReads()).toBe(1);
  });

  it('names the section whose pages moved, with Re-extract in bold inside its sentence', async () => {
    renderSlots(FR_CA);
    await opened();
    await mergeAndSave();
    await waitFor(() => expect(alertHeaded('Pages déplacées dans la section 1')).not.toBeNull());
    const content = alertHeaded('Pages déplacées dans la section 1')!.findContent().getElement();
    expect(content.textContent).toBe(
      'Le regroupement est enregistré et vos valeurs de champs sont intactes, mais ces sections ont été extraites d’un autre ensemble de pages : leurs valeurs pourraient ne plus correspondre. Vérifiez-les, changez la classe de la section et utilisez Réextraire si vous préférez que le modèle les refasse (sur ce document, cela réextrait cette seule section).',
    );
    expect(within(content).getByText('Réextraire').tagName).toBe('B');
  });

  it('says the fields reflect the previous class, around both class names, beside Re-extract for the section', async () => {
    renderSlots(FR_CA, RECLASSIFIED);
    await opened();
    await reclassifyFirstAndSave();
    await waitFor(() => expect(alertHeaded('Les champs reflètent encore la classe précédente')).not.toBeNull());
    const notice = alertHeaded('Les champs reflètent encore la classe précédente')!;
    await waitFor(() =>
      expect(notice.findContent().getElement().textContent).toBe(
        'Ces champs ont été extraits comme Invoice. Réextraire remplace les champs de cette section par ceux que produit le schéma W2; 1 autre section garde ses pages, sa classe et ses champs. Cette section était déjà marquée comme révisée; la réextraire efface ses valeurs confirmées. Changer la classe du document entier fusionnerait ses 2 sections en une seule et effacerait leur vérité terrain; Réextraire ne vise donc ici que cette section.',
      ),
    );
    expect(within(notice.findContent().getElement()).getByText('Invoice').tagName).toBe('B');
    expect(within(notice.findContent().getElement()).getByText('W2').tagName).toBe('B');
    const button = screen.getByRole('button', { name: 'Réextraire la section 1' });
    await waitFor(() => expect(button.textContent).toBe('Réextraire'));
  });

  it('words a re-extract’s result when it shows, and does not run it again on a switch to English', async () => {
    server.reextract = 'polled';
    const view = renderSlots(FR_CA, RECLASSIFIED);
    await opened();
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Réextraire la section 1' });
    await waitFor(() => expect(button.getAttribute('aria-disabled')).not.toBe('true'));
    await userEvent.click(button);
    expect(
      await screen.findByText('Section 1 réextraite comme W2. Les autres sections n’ont pas changé.', undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(callsOf('reextractTestSetDocument')).toBe(1);
    expect(callsOf('getDraftLabelJob')).toBe(1);
    await opened();
    const reads = sectionReads();
    view.setLocale('en');
    expect(await screen.findByText('Re-extracted section 1 as W2. The other sections were not changed.')).toBeInTheDocument();
    expect(callsOf('reextractTestSetDocument')).toBe(1);
    expect(callsOf('getDraftLabelJob')).toBe(1);
    expect(sectionReads()).toBe(reads);
  }, 15000);

  it('words a failed re-extract of the section around the reason it gave', async () => {
    server.reextract = 'failed';
    renderSlots(FR_CA, RECLASSIFIED);
    await opened();
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Réextraire la section 1' });
    await waitFor(() => expect(button.getAttribute('aria-disabled')).not.toBe('true'));
    await userEvent.click(button);
    await waitFor(() => expect(alertHolding('Impossible de réextraire cette section : the run failed')).not.toBeNull());
  });
});

describe('the test-set editor’s section slots under en', () => {
  it('reads today’s English warning after a saved grouping moved pages', async () => {
    renderSlots();
    await opened();
    await mergeAndSave();
    await waitFor(() => expect(alertHeaded('Pages moved in section 1')).not.toBeNull());
    const content = alertHeaded('Pages moved in section 1')!.findContent().getElement();
    expect(content.textContent).toBe(
      "The grouping is saved and your field values are untouched — but those sections were extracted from a different set of pages, so their values may no longer match. Check them, and change the section's class and use Re-extract if you would rather the model redo it (on this document it re-extracts that one section).",
    );
    expect(within(content).getByText('Re-extract').tagName).toBe('B');
  });

  it('reads today’s English notice that the fields reflect the previous class, beside Re-extract for the section', async () => {
    renderSlots({}, RECLASSIFIED);
    await opened();
    await reclassifyFirstAndSave();
    await waitFor(() => expect(alertHeaded('Fields still reflect the previous class')).not.toBeNull());
    const notice = alertHeaded('Fields still reflect the previous class')!;
    await waitFor(() =>
      expect(notice.findContent().getElement().textContent).toBe(
        "These fields were extracted as Invoice. Re-extract replaces this section's fields with ones the W2 schema produces; the other 1 section keeps its pages, class and fields. This section was already marked reviewed; re-extracting discards its confirmed values. Changing the class for the whole document would merge all 2 sections into one and discard their ground truth, so Re-extract here works on this section only.",
      ),
    );
    const button = screen.getByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(button.textContent).toBe('Re-extract'));
  });

  it('reads today’s English note after a re-extract of the section', async () => {
    renderSlots({}, RECLASSIFIED);
    await opened();
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(button.getAttribute('aria-disabled')).not.toBe('true'));
    await userEvent.click(button);
    expect(await screen.findByText('Re-extracted section 1 as W2. The other sections were not changed.')).toBeInTheDocument();
    expect(callsOf('reextractTestSetDocument')).toBe(1);
  });

  it('reads today’s English error after a failed re-extract of the section', async () => {
    server.reextract = 'failed';
    renderSlots({}, RECLASSIFIED);
    await opened();
    await reclassifyFirstAndSave();
    const button = await screen.findByRole('button', { name: 'Re-extract section 1' });
    await waitFor(() => expect(button.getAttribute('aria-disabled')).not.toBe('true'));
    await userEvent.click(button);
    await waitFor(() => expect(alertHolding('Could not re-extract this section: the run failed')).not.toBeNull());
  });
});
