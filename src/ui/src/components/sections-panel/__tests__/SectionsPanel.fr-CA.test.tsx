// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Document Sections table under `fr-CA`: with a fixture catalog its title, the column
 * headers and the column preferences' group of columns (named after the table) read French,
 * and so do a section's status, a processing issue's code label beside the issue's English
 * message (a code the UI has no label for shows as it is), the reason Edit Data is unavailable
 * for a section with no data yet, the popover of a section whose pages appear to hold more
 * records than were extracted, and the notice a failed download shows. Under `en` the same
 * renders read today's English.
 */
import React from 'react';
import { act, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ButtonDropdownWrapper } from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, with the mock set of the other SectionsPanel tests.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  app: { currentCredentials: {}, setErrorMessage: vi.fn() },
  settings: { settings: { OutputBucket: 'out', EvaluationBaselineBucket: 'base' } },
  role: { isReviewerOnly: false, canWrite: true, canReview: true },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../../contexts/settings', () => ({ default: () => state.settings }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));

import SectionsPanel from '../SectionsPanel';
import { SECTION_NO_DATA_REASON } from '../../document-actions/action-reasons';

// Section 1 has data and two issues: a warning whose code the UI labels, and a note whose code
// it has no label for. Section 2 has no data yet, and its pages appear to hold more records
// than were extracted.
const SECTIONS = [
  {
    Id: '1',
    Class: 'Invoice',
    PageIds: [1],
    OutputJSONUri: 's3://out/in/a.pdf/sections/1/result.json',
    ProcessingIssues: [
      {
        stage: 'extraction',
        severity: 'warning',
        code: 'extraction_incomplete',
        message: 'Extraction returned no rows for list field(s): LineItems.',
      },
      { stage: 'extraction', severity: 'info', code: 'unlabelled_issue_code', message: 'A note the UI has no label for.' },
    ],
  },
  {
    Id: '2',
    Class: 'PayStatement',
    PageIds: [2, 3],
    InstanceCount: 4,
    ProcessingIssues: [
      {
        stage: 'extraction',
        severity: 'warning',
        code: 'extraction_multi_instance_suspected',
        message: 'These pages appear to contain 4 separate PayStatement records, but extraction returned 1.',
      },
    ],
  },
];
const DOC = { objectKey: 'in/a.pdf', objectStatus: 'COMPLETED' };

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.sections.title': 'Sections du document',
  'documents.sections.column.id': 'ID de section',
  'documents.sections.column.status': 'Statut',
  'common.columnPreferences.columnsGroup': 'Colonnes : {table}',
  'documents.sectionStatus.degraded': 'Dégradée',
  'documents.sections.issues.header': 'Problèmes de traitement',
  'documents.processingIssue.code.extractionIncomplete': 'Extraction incomplète',
  'documents.sections.row.actionsAriaLabel': 'Actions de la section {section}',
  'documents.sections.row.editData': 'Modifier les données',
  'documentActions.reasons.sectionNoData': 'Cette section n’a pas encore de données',
  'documents.sections.download.predictionFailedWithError': 'Échec du téléchargement des données de prédiction : {error}',
  'documents.sections.multiInstance.suspectedHeader': 'Des enregistrements manquent peut-être dans cette section',
  'documents.sections.multiInstance.suspectedBody':
    'Ces pages semblent contenir {count} enregistrements {className} distincts, mais seul le premier a été extrait : les autres ne figurent pas dans le résultat.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const panel = <SectionsPanel sections={SECTIONS as never} documentItem={DOC as never} mergedConfig={{}} />;

/** The table's column headers. */
const columnHeaders = () =>
  createWrapper()
    .findTable()!
    .findColumnHeaders()
    .map((h) => h.getElement().textContent?.trim());

/** Opens the ⚙ column preferences and returns the label of their group of columns. */
const preferencesGroup = () => {
  const preferences = createWrapper().findTable()!.findCollectionPreferences()!;
  act(() => preferences.findTriggerButton().click());
  return preferences.findModal()!.findVisibleContentPreference()!.findOptionsGroups()[0].getElement().textContent;
};

/** Opens the popover of a body cell (row and column count from 1) and returns its heading and text. */
const openCellPopover = (row: number, column: number) => {
  const popover = createWrapper().findTable()!.findBodyCell(row, column)!.findPopover()!;
  act(() => popover.findTrigger().click());
  return { header: popover.findHeader()!.getElement().textContent, content: popover.findContent()!.getElement().textContent };
};

/** The ⋮ menu of the row whose menu has this screen-reader label, opened. */
const openRowMenu = (ariaLabel: string): ButtonDropdownWrapper => {
  const menu = createWrapper(document.body)
    .findAllButtonDropdowns()
    .find((d) => d.findNativeButton().getElement().getAttribute('aria-label') === ariaLabel)!;
  act(() => menu.openDropdown());
  return menu;
};

/** A menu item's label, and the reason it shows when it is unavailable. */
const menuItem = (menu: ButtonDropdownWrapper, id: string) => {
  const item = menu.findItemById(id)!.getElement();
  const control = item.querySelector('[role="menuitem"]') ?? item;
  const describedBy = control.getAttribute('aria-describedby');
  const reason = describedBy ? (document.getElementById(describedBy)?.textContent ?? '') : '';
  const text = item.textContent ?? '';
  return {
    label: reason && text.endsWith(reason) ? text.slice(0, -reason.length) : text,
    disabled: control.getAttribute('aria-disabled') === 'true',
    reason,
  };
};

// Columns, counted from 1: Section ID, Class/Type, Page IDs, Low Confidence Fields,
// Missing Required Fields, Status, Actions.
const CLASS_COLUMN = 2;
const STATUS_COLUMN = 6;

beforeEach(() => {
  state.graphql.mockReset();
  state.app.setErrorMessage.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the Document Sections table under fr-CA', () => {
  it('reads its title, its column headers and the group of its column preferences in French', async () => {
    renderWithIntl(panel, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Sections du document')).toBeInTheDocument();
    expect(columnHeaders()).toEqual(expect.arrayContaining(['ID de section', 'Statut']));
    const group = preferencesGroup();
    expect(group).toContain('Colonnes : Sections du document');
    expect(group).toContain('ID de section');
    expect(columnHeaders()).not.toContain('Section ID');
  });

  it('reads a section status and an issue code in French, beside the message the backend wrote', async () => {
    renderWithIntl(panel, FR_CA);
    expect(await screen.findByText('Sections du document')).toBeInTheDocument();
    expect(createWrapper().findTable()!.findBodyCell(1, STATUS_COLUMN)!.getElement().textContent).toBe('Dégradée');
    const { header, content } = openCellPopover(1, STATUS_COLUMN);
    expect(header).toBe('Problèmes de traitement');
    expect(content).toContain('Extraction incomplète');
    expect(content).toContain('Extraction returned no rows for list field(s): LineItems.');
    expect(content).not.toContain('extraction_incomplete');
    // A code the UI has no label for shows as it is.
    expect(content).toContain('unlabelled_issue_code');
  });

  it('gives the reason Edit Data is unavailable, and the notice of a failed download, in French', async () => {
    state.graphql.mockRejectedValue(new Error('denied'));
    renderWithIntl(panel, FR_CA);
    expect(await screen.findByText('Sections du document')).toBeInTheDocument();
    expect(menuItem(openRowMenu('Actions de la section 2'), 'edit-data')).toEqual({
      label: 'Modifier les données',
      disabled: true,
      reason: 'Cette section n’a pas encore de données',
    });
    const first = openRowMenu('Actions de la section 1');
    act(() => first.findItemById('prediction')!.click());
    await waitFor(() =>
      expect(state.app.setErrorMessage).toHaveBeenCalledWith('Échec du téléchargement des données de prédiction : denied'),
    );
  });

  it('explains in French a section whose pages appear to hold more records than were extracted', async () => {
    renderWithIntl(panel, FR_CA);
    expect(await screen.findByText('Sections du document')).toBeInTheDocument();
    const { header, content } = openCellPopover(2, CLASS_COLUMN);
    expect(header).toBe('Des enregistrements manquent peut-être dans cette section');
    expect(content).toContain(
      'Ces pages semblent contenir 4 enregistrements PayStatement distincts, mais seul le premier a été extrait : les autres ne figurent pas dans le résultat.',
    );
  });
});

describe('the Document Sections table under en', () => {
  it('reads the inline English for its title, its column headers and the group of its column preferences', () => {
    renderWithIntl(panel);
    expect(createWrapper().findContainer()!.findHeader()!.getElement().textContent).toBe('Document Sections');
    expect(columnHeaders()).toEqual([
      'Section ID',
      'Class/Type',
      'Page IDs',
      'Low Confidence Fields',
      'Missing Required Fields',
      'Status',
      'Actions',
    ]);
    const group = preferencesGroup();
    expect(group).toContain('Document Sections columns');
    expect(group).toContain('Section ID');
  });

  it('reads a section status in English and an issue code as the backend wrote it', () => {
    renderWithIntl(panel);
    expect(createWrapper().findTable()!.findBodyCell(1, STATUS_COLUMN)!.getElement().textContent).toBe('Degraded');
    const { header, content } = openCellPopover(1, STATUS_COLUMN);
    expect(header).toBe('Processing issues');
    expect(content).toContain('extraction_incomplete');
    expect(content).toContain('Extraction returned no rows for list field(s): LineItems.');
    expect(content).toContain('unlabelled_issue_code');
  });

  it('gives the reason Edit Data is unavailable, and the notice of a failed download, in English', async () => {
    state.graphql.mockRejectedValue(new Error('denied'));
    renderWithIntl(panel);
    expect(menuItem(openRowMenu('Actions for section 2'), 'edit-data')).toEqual({
      label: 'Edit Data',
      disabled: true,
      reason: SECTION_NO_DATA_REASON,
    });
    const first = openRowMenu('Actions for section 1');
    act(() => first.findItemById('prediction')!.click());
    await waitFor(() => expect(state.app.setErrorMessage).toHaveBeenCalledWith('Failed to download prediction data: denied'));
  });

  it('explains in English a section whose pages appear to hold more records than were extracted', () => {
    renderWithIntl(panel);
    const { header, content } = openCellPopover(2, CLASS_COLUMN);
    expect(header).toBe('Records may be missing from this section');
    expect(content).toContain(
      'These pages appear to contain 4 separate PayStatement records, but only the first was extracted — the rest are not in the result.',
    );
  });
});
