// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Version History panel and its compare view under `fr-CA`: with a fixture catalog the
 * panel title, a column header and the Current badge read French, and so do a compared
 * section's status and a change type in the compare table. Under `en` the same renders read
 * today's English, where the change type is the value as the versions API returns it.
 */
import React from 'react';
import { act, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render. The panel calls generateClient() at import time,
// so the client's graphql function is hoisted too, as in DocumentVersionsPanel.test.tsx.
const state = vi.hoisted(() => ({ graphql: vi.fn(), role: { isAdmin: false } }));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));

import DocumentVersionsPanel from '../DocumentVersionsPanel';

const VERSIONS = [
  { RunId: '20250707T141530Z-b', CompletionTime: '2025-07-07T14:15:30Z', ConfigVersion: 'v2', PageCount: 6 },
  { RunId: '20250101T090000Z-a', CompletionTime: '2025-01-01T09:00:00Z', ConfigVersion: 'v1', PageCount: 6 },
];

// The comparison of the two versions: one section, whose list of line items grew.
const COMPARISON = {
  objectKey: 'doc.pdf',
  runIdA: '20250101T090000Z-a',
  runIdB: '20250707T141530Z-b',
  configVersionA: 'v1',
  configVersionB: 'v2',
  completionTimeA: '2025-01-01T09:00:00Z',
  completionTimeB: '2025-07-07T14:15:30Z',
  identical: false,
  sections: [{ section: 'Invoice', status: 'changed', changes: [{ path: 'LineItems', type: 'length_changed', a: 2, b: 3 }] }],
};

// Route each query by the operation name embedded in the query string.
const routeGraphql = ({ query }: { query: string }) => {
  if (query.includes('compareDocumentVersions')) {
    // The API returns the comparison as a JSON string.
    return Promise.resolve({ data: { compareDocumentVersions: JSON.stringify(COMPARISON) } });
  }
  if (query.includes('listDocumentVersions')) {
    return Promise.resolve({ data: { listDocumentVersions: VERSIONS } });
  }
  return Promise.resolve({ data: {} });
};

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.versions.title': 'Historique des versions',
  'documents.versions.column.configProfile': 'Profil de configuration',
  'documents.versions.currentBadge': 'Actuelle',
  'documents.versions.compareSelected': 'Comparer la sélection',
  'documents.versions.diffStatus.changed': 'Modifiée',
  'documents.versions.changeType.lengthChanged': 'longueur modifiée',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** The version table's column headers (the compare dialog's table renders later, in a portal). */
const columnHeaders = () =>
  createWrapper()
    .findTable()!
    .findColumnHeaders()
    .map((h) => h.getElement().textContent);

/** Selects both versions and opens the compare dialog with the button labelled `compareLabel`. */
const compareBothVersions = (compareLabel: string) => {
  const table = createWrapper().findTable()!;
  act(() => table.findRowSelectionArea(1)!.click());
  act(() => table.findRowSelectionArea(2)!.click());
  act(() => screen.getByRole('button', { name: compareLabel }).click());
};

beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockImplementation(routeGraphql);
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the Version History panel under fr-CA', () => {
  it('reads its title, a column header and the Current badge in French', async () => {
    renderWithIntl(<DocumentVersionsPanel objectKey="doc.pdf" />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Historique des versions')).toBeInTheDocument();
    expect(await screen.findByText('v2')).toBeInTheDocument();
    expect(columnHeaders()).toContain('Profil de configuration');
    expect(screen.getByText('Actuelle')).toBeInTheDocument();
    expect(screen.queryByText('Version History')).toBeNull();
  });

  it('reads a section status and a change type of the compare view in French', async () => {
    renderWithIntl(<DocumentVersionsPanel objectKey="doc.pdf" />, FR_CA);
    expect(await screen.findByText('Historique des versions')).toBeInTheDocument();
    expect(await screen.findByText('v2')).toBeInTheDocument();
    compareBothVersions('Comparer la sélection');
    expect(await screen.findByText('Modifiée')).toBeInTheDocument();
    expect(screen.getByText('longueur modifiée')).toBeInTheDocument();
    expect(screen.queryByText('length_changed')).toBeNull();
  });
});

describe('the Version History panel under en', () => {
  it('reads the inline English for its title, a column header and the Current badge', async () => {
    renderWithIntl(<DocumentVersionsPanel objectKey="doc.pdf" />);
    expect(await screen.findByText('v2')).toBeInTheDocument();
    expect(screen.getByText('Version History')).toBeInTheDocument();
    expect(columnHeaders()).toContain('Config Profile');
    expect(screen.getByText('Current')).toBeInTheDocument();
  });

  it('reads the compare view in English, the change type as the API returns it', async () => {
    renderWithIntl(<DocumentVersionsPanel objectKey="doc.pdf" />);
    expect(await screen.findByText('v2')).toBeInTheDocument();
    compareBothVersions('Compare selected');
    expect(await screen.findByText('Changed')).toBeInTheDocument();
    expect(screen.getByText('length_changed')).toBeInTheDocument();
  });
});
