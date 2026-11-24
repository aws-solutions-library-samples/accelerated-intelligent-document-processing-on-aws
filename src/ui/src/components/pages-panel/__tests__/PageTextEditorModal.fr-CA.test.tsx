// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The page text dialog and the Source view under `fr-CA`: with a fixture catalog the dialog's
 * read-only title, its view switchers, a toolbar control of the page image viewer inside it and
 * the Source view's loading text read French, and the viewer's zoom level reads `100 %`. Under
 * `en` the same renders read today's English and `100%`.
 */
import React from 'react';
import { screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked module for every render, with the mock set of the signatures test; the
// page image viewer is the real one.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  settings: { settings: {} },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../../graphql/generated', () => ({
  getFileContents: 'getFileContents',
  getFilePresignedUrl: 'getFilePresignedUrl',
  getFilePresignedUrls: 'getFilePresignedUrls',
  uploadDocument: 'uploadDocument',
}));
vi.mock('../../../contexts/settings', () => ({ default: () => state.settings }));
vi.mock('@monaco-editor/react', () => ({ Editor: () => <div data-testid="editor" /> }));
vi.mock('../../document-viewer/MarkdownViewer', () => ({ default: () => <div data-testid="markdown" /> }));

import PageTextEditorModal from '../PageTextEditorModal';
import FileViewer from '../../document-viewer/FileViewer';

// A page whose image needs no signing and which has no OCR page data, so the dialog opens on
// its markdown view, where both view switchers show.
const PAGES = [{ Id: '2', ImageUri: 'https://example.test/doc/pages/2/image.jpg', TextUri: 's3://b/doc/pages/2/result.json' }];

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.pageText.viewTitle': 'Afficher le texte de la page {page}',
  'documents.pageText.view.ocrLines': 'Lignes OCR',
  'documents.pageText.view.rendered': 'Rendu',
  'documents.pageText.view.raw': 'Brut',
  'documents.pageImage.zoomIn': 'Zoom avant',
  'documents.fileViewer.loading': 'Chargement du document...',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const dialog = <PageTextEditorModal visible pages={PAGES} initialPageId="2" />;
const sourceView = <FileViewer objectKey="doc/source.pdf" bucket="b" />;

/** The titles of the dialogs on the page (the unsaved-changes dialog is mounted, hidden). */
const dialogTitles = () =>
  createWrapper()
    .findAllModals()
    .map((modal) => modal.findHeader().getElement().textContent);

/** The labels of the options of every view switcher shown. */
const switcherOptions = () =>
  createWrapper()
    .findAllSegmentedControls()
    .flatMap((control) => control.findSegments().map((segment) => segment.getElement().textContent));

beforeEach(() => {
  // The page's text arrives; the Source view's document never does, so it stays on its loading text.
  state.graphql.mockReset();
  state.graphql.mockImplementation(({ variables }: { variables: { s3Uri: string } }) =>
    variables.s3Uri.endsWith('result.json')
      ? Promise.resolve({ data: { getFileContents: { content: JSON.stringify({ text: '# Page 2' }), isBinary: false } } })
      : new Promise(() => undefined),
  );
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the page text dialog and the Source view under fr-CA', () => {
  it('reads the title, the view switchers and a page image control in French, and the zoom level with the French space', async () => {
    renderWithIntl(dialog, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() => expect(dialogTitles()).toContain('Afficher le texte de la page 2'));
    expect(await screen.findByTitle('Zoom avant')).toBeInTheDocument();
    expect(switcherOptions()).toEqual(expect.arrayContaining(['Lignes OCR', 'Rendu', 'Brut']));
    expect(screen.getByText(/^100\s%$/)).toBeInTheDocument();
    expect(dialogTitles()).not.toContain('View Page 2 Text');
    expect(screen.queryByTitle('Zoom In')).toBeNull();
  });

  it('reads the loading text of the Source view in French', async () => {
    renderWithIntl(sourceView, FR_CA);
    expect(await screen.findByText('Chargement du document...')).toBeInTheDocument();
  });
});

describe('the page text dialog and the Source view under en', () => {
  it('reads the inline English, and the zoom level as before', async () => {
    renderWithIntl(dialog);
    expect(await screen.findByTitle('Zoom In')).toBeInTheDocument();
    expect(dialogTitles()).toContain('View Page 2 Text');
    expect(switcherOptions()).toEqual(expect.arrayContaining(['OCR Lines', 'Rendered', 'Raw']));
    expect(screen.getByText('100%')).toBeInTheDocument();
  });

  it('reads the loading text of the Source view in English', async () => {
    renderWithIntl(sourceView);
    expect(await screen.findByText('Loading document...')).toBeInTheDocument();
  });
});
