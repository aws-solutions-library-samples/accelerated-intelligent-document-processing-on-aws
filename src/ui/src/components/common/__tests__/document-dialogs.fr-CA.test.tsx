// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Delete, Abort and Reprocess dialogs under `fr-CA`: with a fixture catalog their titles
 * read French, each count through its ICU plural, the Abort dialog's `=0` branch included.
 * Under `en` the same renders read today's English for the same counts.
 */
import React from 'react';
import { waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render: the Reprocess dialog's effects depend on them.
const state = vi.hoisted(() => {
  const options = [{ label: 'default (Active)', value: 'default' }];
  return {
    versions: { versions: [{ versionName: 'default', isActive: true }], getVersionOptions: () => options },
    config: { mergedConfig: {}, loading: false, error: null },
    revisions: { revisions: [], profileMatchesPublished: null, loading: false, error: null, loadRevisions: () => undefined },
  };
});

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => state.versions }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => state.config }));
vi.mock('../../../hooks/use-config-profile-revisions', () => ({ default: () => state.revisions }));

import DeleteDocumentModal from '../DeleteDocumentModal';
import AbortWorkflowModal from '../AbortWorkflowModal';
import ReprocessDocumentModal from '../ReprocessDocumentModal';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.deleteModal.title': '{count, plural, =0 {Supprimer le document} one {Supprimer le document} other {Supprimer les documents}}',
  'documents.abortModal.title':
    '{count, plural, =0 {Impossible d’interrompre les flux de travail} one {Interrompre le flux de travail} other {Interrompre # flux de travail}}',
  'documents.reprocessModal.title': '{count, plural, =0 {Retraiter le document} one {Retraiter le document} other {Retraiter # documents}}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const documents = (count: number) => Array.from({ length: count }, (_, i) => ({ objectKey: `in/doc-${i + 1}.pdf` }));
const withStatuses = (statuses: string[]) => statuses.map((objectStatus, i) => ({ objectKey: `in/doc-${i + 1}.pdf`, objectStatus }));

const deleteDialog = (count: number) => (
  <DeleteDocumentModal visible onDismiss={vi.fn()} onConfirm={vi.fn()} selectedItems={documents(count)} />
);
const abortDialog = (statuses: string[]) => (
  <AbortWorkflowModal visible onDismiss={vi.fn()} onConfirm={vi.fn()} selectedItems={withStatuses(statuses)} />
);
const reprocessDialog = (count: number) => (
  <ReprocessDocumentModal visible onDismiss={vi.fn()} onConfirm={vi.fn()} selectedItems={documents(count)} />
);

// The selections behind the Abort dialog's three title forms: no document it can abort, one, three.
const NONE_ABORTABLE = ['COMPLETED'];
const ONE_ABORTABLE = ['RUNNING'];
const THREE_ABORTABLE = ['RUNNING', 'QUEUED', 'OCR'];

/** The open dialog's title. */
const title = (): string | null => createWrapper().findModal()!.findHeader()!.getElement().textContent;

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('the document dialogs under fr-CA', () => {
  it('titles the Delete dialog for one and for three documents', async () => {
    const { rerender } = renderWithIntl(deleteDialog(1), FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() => expect(title()).toBe('Supprimer le document'));
    rerender(deleteDialog(3));
    expect(title()).toBe('Supprimer les documents');
  });

  it('titles the Abort dialog for no, one and three documents it can abort', async () => {
    const { rerender } = renderWithIntl(abortDialog(NONE_ABORTABLE), FR_CA);
    await waitFor(() => expect(title()).toBe('Impossible d’interrompre les flux de travail'));
    rerender(abortDialog(ONE_ABORTABLE));
    expect(title()).toBe('Interrompre le flux de travail');
    rerender(abortDialog(THREE_ABORTABLE));
    expect(title()).toBe('Interrompre 3 flux de travail');
  });

  it('titles the Reprocess dialog', async () => {
    renderWithIntl(reprocessDialog(3), FR_CA);
    await waitFor(() => expect(title()).toBe('Retraiter 3 documents'));
  });
});

describe('the document dialogs under en', () => {
  it('titles the Delete dialog in English for one and for three documents', () => {
    const { rerender } = renderWithIntl(deleteDialog(1));
    expect(title()).toBe('Delete Document');
    rerender(deleteDialog(3));
    expect(title()).toBe('Delete Documents');
  });

  it('titles the Abort dialog in English for no, one and three documents it can abort', () => {
    const { rerender } = renderWithIntl(abortDialog(NONE_ABORTABLE));
    expect(title()).toBe('Cannot abort workflows');
    rerender(abortDialog(ONE_ABORTABLE));
    expect(title()).toBe('Abort workflow');
    rerender(abortDialog(THREE_ABORTABLE));
    expect(title()).toBe('Abort 3 workflows');
  });

  it('titles the Reprocess dialog in English', () => {
    renderWithIntl(reprocessDialog(3));
    expect(title()).toBe('Reprocess 3 documents');
  });
});
