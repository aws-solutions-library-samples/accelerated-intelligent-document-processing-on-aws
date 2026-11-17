// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The document table and the shared table helpers under `fr-CA`: with an `intl` for that locale
 * the column headers, the selection labels, the page sizes, the filter count and the pagination
 * labels come from its catalog; the header's Load button, the circuit-breaker badge and the
 * no-match state render the French when the app's provider carries a fixture catalog. Without
 * `intl`, or under `en`, every one of them gives today's English.
 */
import React from 'react';
import { screen, waitFor } from '@testing-library/react';
import { createIntl } from 'react-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatDateTime } from '../../../i18n/format';
import { onIntlError } from '../../../i18n/LocaleProvider';
import { renderHookWithIntl, renderWithIntl } from '../../../test-utils/renderWithIntl';
import { usePaginationLabels } from '../../common/labels';
import { TableNoMatchState, filterCounterText } from '../../common/table';
import {
  COLUMN_DEFINITIONS_MAIN,
  DocumentsCommonHeader,
  LATEST_PERIODS,
  buildPageSizeOptions,
  selectionLabels,
  type MappedDocument,
} from '../documents-table-config';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
// The badge polls the API for the circuit's state; a closed circuit, loaded, is enough here.
vi.mock('../../../hooks/use-circuit-breaker', () => ({
  default: () => ({
    status: { enabled: true, state: 'CLOSED' },
    loading: false,
    error: null,
    pause: vi.fn(),
    resume: vi.fn(),
    probe: vi.fn(),
  }),
}));
// The badge's panel reads the user's role from the API on mount; it is closed throughout.
vi.mock('../../common/CircuitBreakerPanel', () => ({ default: () => null }));

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.table.column.objectStatus': 'État',
  'documents.table.column.testRunId': 'Exécution de test',
  'documents.table.selection.item': 'sélectionner {name}',
  'documents.table.preferences.pageSizeOption': '{count, plural, one {# document} other {# documents}}',
  'documents.table.load.button': 'Charger : {period}',
  'documents.table.load.latest': 'Dernières',
  'documents.table.download.label': 'Télécharger',
  'documents.list.circuit.closed': 'Circuit : fermé',
  'common.pagination.page': 'Page {pageNumber, number} de toutes les pages',
  'common.table.filterCount': '{count, plural, one {# correspondance} other {# correspondances}}',
  'common.table.noMatch.title': 'Aucune correspondance',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const fr = createIntl({ locale: 'fr-CA', defaultLocale: 'en', messages: FR, onError: onIntlError });
const en = createIntl({ locale: 'en', defaultLocale: 'en' });

const row = { objectKey: 'a.pdf' } as MappedDocument;
const headerOf = (columns: ReturnType<typeof COLUMN_DEFINITIONS_MAIN>, id: string) => columns.find((c) => c.id === id)?.header;

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the document table helpers under fr-CA', () => {
  it('name the columns from the French catalog', () => {
    const columns = COLUMN_DEFINITIONS_MAIN([], 'TEST', false, fr);
    expect(headerOf(columns, 'objectStatus')).toBe('État');
    expect(headerOf(columns, 'testRunId')).toBe('Exécution de test');
  });

  it('label the row checkboxes and the page sizes in French', () => {
    expect(selectionLabels(fr).itemSelectionLabel?.({ selectedItems: [] }, row)).toBe('sélectionner a.pdf');
    expect(buildPageSizeOptions(fr).map((o) => o.label)).toEqual(['10 documents', '30 documents', '50 documents']);
  });

  it('count the filter matches with the French plural', () => {
    expect(filterCounterText(fr, 1)).toBe('1 correspondance');
    expect(filterCounterText(fr, 2)).toBe('2 correspondances');
  });

  it('give the pagination labels from the catalog of the active locale', async () => {
    const { result } = renderHookWithIntl(() => usePaginationLabels(), FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() => expect(result.current.pageLabel?.(1)).toBe('Page 1 de toutes les pages'));
  });

  it('render the no-match state in French', async () => {
    renderWithIntl(<TableNoMatchState />, FR_CA);
    expect(await screen.findByText('Aucune correspondance')).toBeInTheDocument();
    expect(screen.queryByText('No matches')).toBeNull();
  });

  it('render the Load button, the Download menu and the circuit badge of the header in French', async () => {
    renderWithIntl(<DocumentsCommonHeader periodsToLoad={LATEST_PERIODS} />, FR_CA);
    expect(await screen.findByText('Charger : Dernières')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Télécharger' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Circuit : fermé' })).toBeInTheDocument();
    expect(screen.queryByText('Load: Latest')).toBeNull();
  });
});

describe('the document table helpers in English', () => {
  it('are the English source text without intl, as the bare callers get them', () => {
    expect(headerOf(COLUMN_DEFINITIONS_MAIN(), 'objectStatus')).toBe('Status');
    expect(headerOf(COLUMN_DEFINITIONS_MAIN([], 'TEST'), 'testRunId')).toBe('Test Run');
  });

  it('read today’s English under en', () => {
    expect(headerOf(COLUMN_DEFINITIONS_MAIN([], 'PRODUCTION', false, en), 'objectStatus')).toBe('Status');
    expect(selectionLabels(en).itemSelectionLabel?.({ selectedItems: [] }, row)).toBe('select a.pdf');
    expect(selectionLabels(en).selectionGroupLabel).toBe('Document selection');
    expect(buildPageSizeOptions(en).map((o) => o.label)).toEqual(['10 Documents', '30 Documents', '50 Documents']);
    expect(filterCounterText(en, 1)).toBe('1 match');
    expect(filterCounterText(en, 2)).toBe('2 matches');
  });

  it('give the English pagination labels under en', () => {
    const { result } = renderHookWithIntl(() => usePaginationLabels());
    expect(result.current.nextPageLabel).toBe('Next page');
    expect(result.current.previousPageLabel).toBe('Previous page');
    expect(result.current.pageLabel?.(1)).toBe('Page 1 of all pages');
  });

  it('render the header in English under en', () => {
    renderWithIntl(<DocumentsCommonHeader periodsToLoad={LATEST_PERIODS} />);
    expect(screen.getByText('Load: Latest')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Circuit: closed' })).toBeInTheDocument();
  });
});

describe('the Submitted cell', () => {
  /** A fixed instant, stored as the list maps it: the UTC ISO string. */
  const SUBMITTED = new Date(1_700_000_000_000).toISOString();
  const submittedCell = (initialEventTime: string) =>
    COLUMN_DEFINITIONS_MAIN()
      .find((c) => c.id === 'initialEventTime')
      ?.cell({ ...row, initialEventTime });

  it('shows the stored time in the UI locale, and no time as nothing', async () => {
    const french = renderWithIntl(submittedCell(SUBMITTED), FR_CA).container;
    const english = renderWithIntl(submittedCell(SUBMITTED)).container;
    const empty = renderWithIntl(submittedCell('')).container;
    // The helper is the oracle: the exact shape of each locale belongs to ICU. One comparison,
    // so a cell that ignores the formatter shows every field it gets wrong. Awaited so the
    // French catalog settles inside the test.
    await waitFor(() =>
      expect({ french: french.textContent, english: english.textContent, empty: empty.textContent }).toEqual({
        french: formatDateTime(new Date(SUBMITTED), 'fr-CA'),
        english: formatDateTime(new Date(SUBMITTED), 'en'),
        empty: '',
      }),
    );
    // Not vacuous: the French text is not the English text.
    expect(french.textContent).not.toBe(english.textContent);
  });
});
