// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * A document's edit page (the direct edit) under `fr-CA`: with a fixture catalog the
 * breadcrumbs from the document page and the status after the title read French, and the
 * Home crumb the page shows while the document loads, when it no longer exists and when it
 * could not be read reads French too. An empty status stays empty rather than reading
 * Unknown. Under `en` the same renders read today's English.
 */
import React from 'react';
import { waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  app: { user: { username: 'alice', signInDetails: { loginId: 'alice@example.com' } }, setSuccessMessage: vi.fn() },
  role: {
    isAdmin: true,
    isAuthor: false,
    isReviewer: false,
    isAnnotator: false,
    isReviewerOnly: false,
    canWrite: true,
    canReview: true,
    canWorkReviewQueues: true,
    isAnnotatorOnly: false,
  },
  evaluation: { available: false, baseline: null },
  /** The breadcrumbs the page last gave its layout. */
  trail: null as { text?: string; href?: string }[] | null,
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => ({ mergedConfig: { classes: [] } }) }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => ({ urls: {}, failure: null }) }));
vi.mock('../use-section-evaluation', () => ({ default: () => state.evaluation }));
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({
  default: ({ children, trail }: { children: React.ReactNode; trail?: { text?: string; href?: string }[] }) => {
    state.trail = trail ?? null;
    return <>{children}</>;
  },
}));
vi.mock('../SectionEditor', () => ({ default: () => <div data-testid="editor" /> }));

import DocumentEdit from '../DocumentEdit';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'breadcrumbs.documentList.root': 'Traitement de documents',
  'breadcrumbs.documentList.documents': 'Documents',
  'breadcrumbs.reviewStudio.edit': 'Modifier',
  'breadcrumbs.reviewStudio.home': 'Accueil',
  'documents.status.completed': 'TERMINÉ',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
type IntlOptions = Parameters<typeof renderWithIntl>[1];

const rawDoc = (over: Record<string, unknown> = {}) => ({
  ObjectKey: 'in/doc.pdf',
  ObjectStatus: 'COMPLETED',
  Sections: [{ Id: '1', Class: 'W2', PageIds: [1], OutputJSONUri: 's3://out/in/doc.pdf/sections/1/result.json' }],
  Pages: [{ Id: 1, ImageUri: 's3://out/in/doc.pdf/pages/1/image.jpg' }],
  ...over,
});

/** The document reads that leave the page with no document: not answered yet, no document, an error. */
const NO_DOCUMENT: [string, () => Promise<unknown>][] = [
  ['while the document loads', () => new Promise(() => {})],
  ['when the document no longer exists', async () => ({ data: { getDocument: null } })],
  [
    'when the document could not be read',
    async () => {
      throw new Error('offline');
    },
  ],
];

const renderPage = (options: IntlOptions = {}) =>
  renderWithIntl(
    <MemoryRouter initialEntries={['/documents/in%2Fdoc.pdf/edit?from=document']}>
      <DocumentEdit objectKey="in/doc.pdf" />
    </MemoryRouter>,
    options,
  );

const trailTexts = () => state.trail?.map((c) => c.text);
const heading = () => createWrapper(document.body).findHeader()?.findHeadingText().getElement().textContent;

beforeEach(() => {
  document.body.innerHTML = '';
  state.trail = null;
  state.graphql.mockReset().mockResolvedValue({ data: { getDocument: rawDoc() } });
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('a document’s edit page under fr-CA', () => {
  it('words the breadcrumbs from the document page and the status after the title', async () => {
    renderPage(FR_CA);
    await waitFor(() => expect(heading()).toBe('doc.pdf (TERMINÉ)'));
    expect(trailTexts()).toEqual(['Traitement de documents', 'Documents', 'doc.pdf', 'Modifier']);
  });

  it('leaves an empty status out of the title rather than reading Unknown', async () => {
    state.graphql.mockResolvedValue({ data: { getDocument: rawDoc({ ObjectStatus: undefined }) } });
    renderPage(FR_CA);
    await waitFor(() => expect(trailTexts()).toEqual(['Traitement de documents', 'Documents', 'doc.pdf', 'Modifier']));
    expect(heading()).toBe('doc.pdf');
  });

  it.each(NO_DOCUMENT)('words the Home crumb %s', async (_when, read) => {
    state.graphql.mockImplementation(read);
    renderPage(FR_CA);
    await waitFor(() => expect(state.trail).toEqual([{ text: 'Accueil', href: '#/' }]));
  });
});

describe('a document’s edit page under en', () => {
  it('reads today’s English for the breadcrumbs from the document page and the status', async () => {
    renderPage();
    await waitFor(() => expect(heading()).toBe('doc.pdf (COMPLETED)'));
    expect(trailTexts()).toEqual(['Document Processing', 'Documents', 'doc.pdf', 'Edit']);
  });

  it.each(NO_DOCUMENT)('reads today’s English Home crumb %s', async (_when, read) => {
    state.graphql.mockImplementation(read);
    renderPage();
    await waitFor(() => expect(state.trail).toEqual([{ text: 'Home', href: '#/' }]));
  });
});
