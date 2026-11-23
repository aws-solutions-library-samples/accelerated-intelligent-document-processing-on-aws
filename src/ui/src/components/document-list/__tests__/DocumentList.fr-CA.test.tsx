// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The document list page and its help panel under `fr-CA`: with a fixture catalog the page's
 * title, its search field and its empty state read French, and so do the help panel's heading
 * and documentation links, which keep their docs-site `href`. Under `en` both read today's
 * English.
 */
import React from 'react';
import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One list for every render: the page re-maps its rows whenever the context's `documents` changes identity.
const { NO_DOCUMENTS } = vi.hoisted(() => ({ NO_DOCUMENTS: [] as Record<string, unknown>[] }));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
// The smallest set of mocks that mounts the page: the contexts and the API client it reads on
// mount, with no documents loaded so the empty state shows.
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../api/auth-session', () => ({
  fetchSharedAuthSession: () => Promise.resolve({ tokens: { idToken: { payload: { 'cognito:username': 'author' } } } }),
}));
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => vi.fn(),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: null }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: false, isReviewerOnly: false, canWrite: true, canReview: false, canWorkReviewQueues: false }),
}));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
vi.mock('../CircuitBreakerBadge', () => ({ default: () => <span /> }));
vi.mock('../../common/ReprocessDocumentModal', () => ({ default: () => null }));
vi.mock('../../../contexts/documents', () => ({
  default: () => ({
    documents: NO_DOCUMENTS,
    isDocumentsListLoading: false,
    hasListBeenLoaded: true,
    setIsDocumentsListLoading: vi.fn(),
    setPeriodsToLoad: vi.fn(),
    setSelectedItems: vi.fn(),
    setToolsOpen: vi.fn(),
    periodsToLoad: 1,
    customDateRange: null,
    setCustomDateRange: vi.fn(),
    documentView: 'PRODUCTION',
    setDocumentView: vi.fn(),
    latestTruncated: false,
    getDocumentDetailsFromIds: vi.fn(),
    deleteDocuments: vi.fn(),
    reprocessDocuments: vi.fn(),
    abortWorkflows: vi.fn(),
  }),
}));

import DocumentList from '../DocumentList';
import DocumentListToolsPanel from '../tools-panel';

const DOCS_WEB_UI = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/web-ui/';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.list.title': 'Documents (fr)',
  'documents.list.filterPlaceholder': 'Rechercher des documents',
  'documents.list.empty.title': 'Aucun document',
  'helpPanel.documents.title': 'Documents (aide)',
  'helpPanel.documents.docsWebUi': 'Interface Web',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the Documents help panel under a locale', () => {
  it('reads its heading and link labels from the French catalog, the links keeping their docs href', async () => {
    renderWithIntl(<DocumentListToolsPanel />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByRole('heading', { name: 'Documents (aide)' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Interface Web' })).toHaveAttribute('href', DOCS_WEB_UI);
    expect(screen.queryByText('Web UI')).toBeNull();
  });

  it('reads the inline English under en', () => {
    renderWithIntl(<DocumentListToolsPanel />);
    expect(screen.getByRole('heading', { name: 'Documents' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Web UI' })).toHaveAttribute('href', DOCS_WEB_UI);
  });
});

describe('the document list page under a locale', () => {
  it('titles the page, labels the search field and names the empty state in French', async () => {
    renderWithIntl(<DocumentList />, FR_CA);
    expect(await screen.findByText('Documents (fr)')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Rechercher des documents')).toBeInTheDocument();
    expect(screen.getByText('Aucun document')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Find documents')).toBeNull();
  });

  it('reads the inline English under en', () => {
    renderWithIntl(<DocumentList />);
    expect(screen.getByPlaceholderText('Find documents')).toBeInTheDocument();
    expect(screen.getByText('No documents')).toBeInTheDocument();
  });
});
