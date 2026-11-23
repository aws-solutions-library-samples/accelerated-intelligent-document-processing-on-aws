// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Document Pages table under `fr-CA`: with a fixture catalog a column header, the column
 * preferences' title and their group of columns (named after the table), the class-confidence
 * link of a page that has an explanation but no score, and the alert for refused thumbnails
 * read French. Under `en` the same render reads today's English.
 */
import React from 'react';
import { act, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, with the mock set of the other PagesPanel tests.
const state = vi.hoisted(() => ({
  presignBatch: vi.fn(),
  app: { currentCredentials: {} },
  settings: { settings: {} },
  role: { isReviewerOnly: false, canWrite: true, canReview: true },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../api/presign-batch', () => ({ presignBatch: state.presignBatch }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../../contexts/settings', () => ({ default: () => state.settings }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));

import PagesPanel from '../PagesPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';

// A page with the classifier's reasoning but no score, so its confidence cell is the Why? link.
const PAGES = [{ Id: '1', Class: 'Invoice', ClassReason: 'Remittance block present', ImageUri: 's3://b/doc/pages/1/image.jpg' }];

/** A presign result for a document the resolver refused: no URL for any page. */
const REFUSED = { get: () => undefined, size: 0, failed: true, accessDenied: true };

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.pages.title': 'Pages du document',
  'documents.pages.column.classConfidence': 'Confiance de la classe',
  'documents.pages.thumbnailsUnavailable': 'Miniatures des pages non disponibles',
  'common.columnPreferences.title': 'Préférences du tableau',
  'common.columnPreferences.columnsGroup': 'Colonnes : {table}',
  'common.fileAccess.denied': 'Vous n’avez pas l’autorisation de consulter ce fichier.',
  'documents.classConfidence.why': 'Pourquoi?',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const panel = (
  <DocumentVersionProvider runId={null} files={[]}>
    <PagesPanel pages={PAGES} />
  </DocumentVersionProvider>
);

/** The table's column headers. */
const columnHeaders = () =>
  createWrapper()
    .findTable()!
    .findColumnHeaders()
    .map((h) => h.getElement().textContent?.trim());

/** Opens the ⚙ column preferences and returns the dialog's heading and the label of its group of columns. */
const openPreferences = () => {
  const preferences = createWrapper().findTable()!.findCollectionPreferences()!;
  act(() => preferences.findTriggerButton().click());
  const modal = preferences.findModal()!;
  return {
    heading: modal.findHeader().getElement().textContent,
    group: modal.findVisibleContentPreference()!.findOptionsGroups()[0].getElement().textContent,
  };
};

beforeEach(() => {
  state.presignBatch.mockReset();
  state.presignBatch.mockResolvedValue(REFUSED);
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the Document Pages table under fr-CA', () => {
  it('reads its title, a column header, the column preferences and the Why? link in French', async () => {
    renderWithIntl(panel, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Pages du document')).toBeInTheDocument();
    expect(columnHeaders()).toContain('Confiance de la classe');
    expect(screen.getByRole('button', { name: 'Pourquoi?' })).toBeInTheDocument();
    const { heading, group } = openPreferences();
    expect(heading).toBe('Préférences du tableau');
    expect(group).toContain('Colonnes : Pages du document');
    expect(group).toContain('Confiance de la classe');
    expect(screen.queryByText('Why?')).toBeNull();
  });

  it('reads the alert for refused thumbnails in French', async () => {
    renderWithIntl(panel, FR_CA);
    expect(await screen.findByText('Vous n’avez pas l’autorisation de consulter ce fichier.')).toBeInTheDocument();
    expect(screen.getByText('Miniatures des pages non disponibles')).toBeInTheDocument();
    expect(screen.queryByText('Page thumbnails unavailable')).toBeNull();
  });
});

describe('the Document Pages table under en', () => {
  it('reads the inline English for its title, a column header, the column preferences and the Why? link', () => {
    renderWithIntl(panel);
    expect(screen.getByText('Document Pages')).toBeInTheDocument();
    expect(columnHeaders()).toContain('Class Confidence');
    expect(screen.getByRole('button', { name: 'Why?' })).toBeInTheDocument();
    const { heading, group } = openPreferences();
    expect(heading).toBe('Preferences');
    expect(group).toContain('Document Pages columns');
    expect(group).toContain('Class Confidence');
  });

  it('reads the alert for refused thumbnails in English', async () => {
    renderWithIntl(panel);
    expect(
      await screen.findByText(
        'You do not have permission to view this file. If your account is new, ask an administrator to assign it a role.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('Page thumbnails unavailable')).toBeInTheDocument();
  });
});
