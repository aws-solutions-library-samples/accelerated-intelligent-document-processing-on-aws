// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The upload page and its help panel under `fr-CA`: with a fixture catalog the file picker's
 * button and the document source control read French, the app's own file picker text winning
 * over Cloudscape's `fr` bundle (which a bare file picker beside the page shows); the submit
 * button's count is an ICU plural; the help panel's heading and supported-formats line read
 * French, its links keeping their docs-site `href`. Under `en` the same texts read English.
 */
import React from 'react';
import { screen } from '@testing-library/react';
import { FileUpload } from '@cloudscape-design/components';
import { createIntl } from 'react-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { onIntlError } from '../../../i18n/LocaleProvider';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per hook for every render: the panel re-runs its effects when one changes identity.
const { SETTINGS, CONFIG_VERSIONS, SAMPLE_DOCUMENTS, CONFIG_LIBRARY, PREFIX_MAPPINGS } = vi.hoisted(() => {
  const versionOptions = [{ label: 'default (Active)', value: 'default' }];
  return {
    SETTINGS: { settings: { InputBucket: 'input-bucket' } },
    CONFIG_VERSIONS: {
      versions: [{ versionName: 'default', isActive: true }],
      getVersionOptions: () => versionOptions,
      saveAsNewVersion: vi.fn(),
    },
    SAMPLE_DOCUMENTS: { listSamples: vi.fn(async () => []), uploadSample: vi.fn() },
    CONFIG_LIBRARY: { getFile: vi.fn() },
    PREFIX_MAPPINGS: { previewAssignment: vi.fn(async () => null) },
  };
});

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
// The mock set of UploadDocumentPanel.test.tsx, with no prefix mapping matched.
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => SETTINGS }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => CONFIG_VERSIONS }));
vi.mock('../../../hooks/use-sample-documents', () => ({ default: () => SAMPLE_DOCUMENTS }));
vi.mock('../../../hooks/use-configuration-library', () => ({ default: () => CONFIG_LIBRARY }));
vi.mock('../../common/ConfigRevisionSelector', () => ({ default: () => null }));
vi.mock('../../../hooks/use-config-prefix-mappings', () => ({ default: () => PREFIX_MAPPINGS }));

import UploadDocumentPanel, { submitMessages } from '../UploadDocumentPanel';
import UploadToolsPanel from '../tools-panel';

const DOCS = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  // Cloudscape's bundle says "Choisir les fichiers"; a different fixture shows whose text the button reads.
  'documents.upload.files.chooseMultiple': 'Choisir des fichiers',
  'documents.upload.source.local': 'Depuis mon ordinateur',
  'documents.upload.source.sample': 'Exemples de documents',
  'documents.upload.submit.upload': '{count, plural, =0 {Téléverser} one {Téléverser (# fichier)} other {Téléverser (# fichiers)}}',
  'helpPanel.upload.title': 'Téléverser des documents',
  'helpPanel.upload.docsWebUi': 'Interface Web',
  'helpPanel.upload.docsOcrSizing': 'Guide de l’OCR et de la taille des images',
  'helpPanel.upload.formats': 'Formats pris en charge : PDF, PNG, JPEG, TIFF, Excel (XLSX/XLS), CSV, Word (DOCX) et texte brut (TXT).',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const fr = createIntl({ locale: 'fr-CA', defaultLocale: 'en', messages: FR, onError: onIntlError });
const en = createIntl({ locale: 'en', defaultLocale: 'en' });

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the upload page under fr-CA', () => {
  it('names the file picker button from the app catalog, over the Cloudscape bundle', async () => {
    renderWithIntl(
      <>
        <UploadDocumentPanel />
        <FileUpload value={[]} onChange={() => undefined} multiple />
      </>,
      FR_CA,
    );
    // Awaited: the catalog and the bundle arrive asynchronously, as their chunks do in the browser.
    // The picker's button is decorative (aria-hidden); its file input is named by the button text.
    // The bare picker has no text of its own, so it shows the bundle's: the bundle is loaded.
    expect(await screen.findByLabelText('Choisir les fichiers')).toBeInTheDocument();
    expect(await screen.findByLabelText('Choisir des fichiers')).toBeInTheDocument();
    expect(screen.queryByLabelText('Choose files')).toBeNull();
  });

  it('labels the document source options and the empty upload button in French', async () => {
    renderWithIntl(<UploadDocumentPanel />, FR_CA);
    expect(await screen.findByRole('button', { name: 'Depuis mon ordinateur', pressed: true })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Exemples de documents', pressed: false })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Téléverser' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'From my computer', pressed: true })).toBeNull();
  });

  it('counts the chosen files with the French plural', () => {
    // Formatted from the panel's descriptor: this test does not choose files in the picker.
    expect(fr.formatMessage(submitMessages.upload, { count: 0 })).toBe('Téléverser');
    expect(fr.formatMessage(submitMessages.upload, { count: 1 })).toBe('Téléverser (1 fichier)');
    expect(fr.formatMessage(submitMessages.upload, { count: 2 })).toBe('Téléverser (2 fichiers)');
  });
});

describe('the Upload Documents help panel under fr-CA', () => {
  it('reads its heading and the supported formats from the French catalog, the links keeping their docs href', async () => {
    renderWithIntl(<UploadToolsPanel />, FR_CA);
    expect(await screen.findByRole('heading', { name: 'Téléverser des documents' })).toBeInTheDocument();
    expect(screen.getByText(FR['helpPanel.upload.formats'])).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Interface Web' })).toHaveAttribute('href', `${DOCS}/web-ui/`);
    expect(screen.getByRole('link', { name: FR['helpPanel.upload.docsOcrSizing'] })).toHaveAttribute(
      'href',
      `${DOCS}/ocr-image-sizing-guide/`,
    );
    expect(screen.queryByText(/^Supported formats:/)).toBeNull();
  });
});

describe('the upload page in English', () => {
  it('counts the chosen files as Upload, one file, files', () => {
    expect(en.formatMessage(submitMessages.upload, { count: 0 })).toBe('Upload');
    expect(en.formatMessage(submitMessages.upload, { count: 1 })).toBe('Upload (1 file)');
    expect(en.formatMessage(submitMessages.upload, { count: 2 })).toBe('Upload (2 files)');
  });

  it('reads the inline English under en', () => {
    renderWithIntl(<UploadDocumentPanel />);
    expect(screen.getByLabelText('Choose files')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'From my computer', pressed: true })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload' })).toBeInTheDocument();
  });
});
