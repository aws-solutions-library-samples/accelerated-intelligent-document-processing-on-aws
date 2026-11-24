// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The download dialogs under `fr-CA`: with a fixture catalog the options dialog's titles for one
 * document and for several, and the progress dialog's skipped-items heading and files counter for
 * one and for two read French, and so does the archive stage, its percentage with the French
 * space (`45 %`). Under `en` the same renders read today's English, except that both counts are
 * now singular for one (`1 item`, `0 of 1 file`).
 */
import React from 'react';
import { screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { DownloadOptionsModal, DownloadProgressModal } from '../DocumentDownloadModals';
import type { ExportErrorEntry, ExportProgress } from '../document-export';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.downloadOptions.title': 'Télécharger toutes les données du document',
  'documents.downloadOptions.titleBulk': 'Télécharger les données de {count, plural, one {# document} other {# documents}}',
  'documents.downloadProgress.errorsTitle':
    '{count, plural, one {# élément n’a pas pu être inclus} other {# éléments n’ont pas pu être inclus}}',
  'documents.downloadProgress.files': '{completed, number} sur {total, plural, one {# fichier traité} other {# fichiers traités}}',
  'documents.downloadProgress.stage.generatingPercent': 'Génération de l’archive… {ratio, number, ::percent}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const noop = (): void => undefined;

/** The options dialog, for one document when `documentCount` is left out. */
const OptionsDialog = ({ documentCount }: { documentCount?: number }): React.JSX.Element => (
  <DownloadOptionsModal
    visible
    includePageImages={false}
    includeSourceDocument={false}
    onIncludePageImagesChange={noop}
    onIncludeSourceDocumentChange={noop}
    onConfirm={noop}
    onDismiss={noop}
    documentCount={documentCount}
  />
);

/** The progress dialog of a one-document export of `total` steps, none done yet, with `skipped` files left out. */
const ProgressDialog = ({
  total,
  skipped,
  stage,
  percent,
}: { total: number; skipped: number } & Pick<ExportProgress, 'stage' | 'percent'>): React.JSX.Element => {
  const errors: ExportErrorEntry[] = Array.from({ length: skipped }, (_, i) => ({
    path: `pages/${i + 1}/image.jpg`,
    message: 'HTTP 403 Forbidden',
  }));
  return (
    <DownloadProgressModal
      visible
      progress={{ completed: 0, total, currentFile: '', errors, stage, percent }}
      errors={errors}
      isFinished={false}
      onCancel={noop}
      onClose={noop}
    />
  );
};

const optionsDialogs = (
  <>
    <OptionsDialog />
    <OptionsDialog documentCount={3} />
  </>
);

// One file and one skipped item while the archive is 45 % built; two of each at the start.
const progressDialogs = (
  <>
    <ProgressDialog total={1} skipped={1} stage="generating" percent={45} />
    <ProgressDialog total={2} skipped={2} />
  </>
);

const modalTitles = () =>
  createWrapper()
    .findAllModals()
    .map((modal) => modal.findHeader().getElement().textContent);

const alertTitles = () =>
  createWrapper()
    .findAllAlerts()
    .map((alert) => alert.findHeader()?.getElement().textContent);

/** The line under each progress bar: the export's stage, or the file being processed. */
const stageLines = () =>
  createWrapper()
    .findAllProgressBars()
    .map((bar) => bar.findAdditionalInfo()?.getElement().textContent);

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the download dialogs under fr-CA', () => {
  it('titles the options dialog for one document and for several in French', async () => {
    renderWithIntl(optionsDialogs, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() =>
      expect(modalTitles()).toEqual(
        expect.arrayContaining(['Télécharger toutes les données du document', 'Télécharger les données de 3 documents']),
      ),
    );
    expect(modalTitles()).not.toContain('Download data for 3 documents');
  });

  it('counts the skipped items and the files for one and for two, and words the archive stage with the French space', async () => {
    renderWithIntl(progressDialogs, FR_CA);
    await waitFor(() =>
      expect(alertTitles()).toEqual(expect.arrayContaining(['1 élément n’a pas pu être inclus', '2 éléments n’ont pas pu être inclus'])),
    );
    expect(screen.getByText('0 sur 1 fichier traité')).toBeInTheDocument();
    expect(screen.getByText('0 sur 2 fichiers traités')).toBeInTheDocument();
    expect(stageLines()).toContainEqual(expect.stringMatching(/^Génération de l’archive… 45\s%$/));
  });
});

describe('the download dialogs under en', () => {
  it('titles the options dialog as before', () => {
    renderWithIntl(optionsDialogs);
    expect(modalTitles()).toEqual(expect.arrayContaining(['Download all document data', 'Download data for 3 documents']));
  });

  it('reads one item and one file in the singular, and the archive stage as before', () => {
    renderWithIntl(progressDialogs);
    expect(alertTitles()).toEqual(expect.arrayContaining(['1 item could not be included', '2 items could not be included']));
    expect(screen.getByText('0 of 1 file processed')).toBeInTheDocument();
    expect(screen.getByText('0 of 2 files processed')).toBeInTheDocument();
    expect(stageLines()).toContain('Generating archive… 45%');
  });
});
