// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The document page's downloads, a group in More Actions: the whole document, its
// predictions or its baselines as a ZIP, with the options dialog (Download All) and the
// progress dialog.
import React, { useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import type { ActionBarMenu } from '../common/ActionBar';
import { DownloadOptionsModal, DownloadProgressModal } from '../document-panel/DocumentDownloadModals';
import { exportDocument, isBaselineAvailable, triggerBrowserDownload } from '../document-panel/document-export';
import type { ExportErrorEntry, ExportProgress, ExportScope } from '../document-panel/document-export';

const logger = new ConsoleLogger('DocumentDownload');

const messages = defineMessages<{
  download: NoMessageValues;
  downloadAll: NoMessageValues;
  downloadPredictions: NoMessageValues;
  downloadBaselines: NoMessageValues;
  failed: { readonly error: string };
  unknownError: NoMessageValues;
}>({
  download: {
    id: 'documentActions.download.menu',
    defaultMessage: 'Download',
    description: "Name of the document page's downloads menu, whose items show as a group in the More Actions menu.",
  },
  downloadAll: {
    id: 'documentActions.download.all',
    defaultMessage: 'Download All (ZIP)',
    description: "Item of the document page's More Actions menu: downloads the document's files as a ZIP, after asking what to include.",
  },
  downloadPredictions: {
    id: 'documentActions.download.predictions',
    defaultMessage: 'Download Predictions (ZIP)',
    description: "Item of the document page's More Actions menu: downloads the document's extraction results as a ZIP.",
  },
  downloadBaselines: {
    id: 'documentActions.download.baselines',
    defaultMessage: 'Download Baselines (ZIP)',
    description: "Item of the document page's More Actions menu: downloads the document's evaluation baseline as a ZIP.",
  },
  failed: {
    id: 'documentActions.download.failed',
    defaultMessage: 'Download failed: {error}',
    description:
      'Error notification when a download from the document page failed. {error} is the reason as the browser or the server gave it, or "Unknown error".',
  },
  unknownError: {
    id: 'documentActions.download.unknownError',
    defaultMessage: 'Unknown error',
    description: 'Stands for the reason in "Download failed: …" when the failure gave none.',
  },
});

interface DocumentDownload {
  /** The downloads, as a menu: More Actions takes its items as a group, and its onItemClick for them. */
  menu: ActionBarMenu;
  /** The options and progress dialogs; render them with the page's dialogs. */
  dialogs: React.ReactNode;
}

export const useDocumentDownload = (
  item: Parameters<typeof isBaselineAvailable>[0] & { objectKey: string },
  settings: unknown,
  credentials: unknown,
  setErrorMessage: (message: string) => void,
): DocumentDownload => {
  const intl = useIntl();
  const [downloadScope, setDownloadScope] = useState<ExportScope | null>(null);
  const [isDownloadOptionsOpen, setIsDownloadOptionsOpen] = useState(false);
  const [includePageImages, setIncludePageImages] = useState(false);
  const [includeSourceDocument, setIncludeSourceDocument] = useState(false);
  const [isDownloadInProgress, setIsDownloadInProgress] = useState(false);
  const [isDownloadFinished, setIsDownloadFinished] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<ExportProgress | null>(null);
  const [downloadErrors, setDownloadErrors] = useState<ExportErrorEntry[]>([]);
  const [downloadAbortController, setDownloadAbortController] = useState<AbortController | null>(null);

  // Kick off a document export for the given scope. Scope 'all' routes through the
  // options modal so the user can opt into page images; other scopes start immediately.
  const startDownload = async (scope: ExportScope) => {
    setDownloadScope(scope);
    setDownloadErrors([]);
    setDownloadProgress(null);
    setIsDownloadFinished(false);
    setIsDownloadInProgress(true);
    const controller = new AbortController();
    setDownloadAbortController(controller);

    try {
      const result = await exportDocument(
        item as unknown as Parameters<typeof exportDocument>[0],
        settings as Parameters<typeof exportDocument>[1],
        {
          scope,
          includePageImages: scope === 'all' && includePageImages,
          includeSourceDocument: scope === 'all' && includeSourceDocument,
          credentials: credentials as Record<string, unknown>,
          signal: controller.signal,
          onProgress: (p) => {
            setDownloadProgress(p);
            setDownloadErrors(p.errors);
          },
        },
      );
      triggerBrowserDownload(result);
      setDownloadErrors(result.errors);
    } catch (err) {
      if ((err as { name?: string })?.name === 'AbortError') {
        logger.info('Document export cancelled by user');
      } else {
        logger.error('Document export failed:', err);
        setErrorMessage(
          intl.formatMessage(messages.failed, { error: (err as Error).message || intl.formatMessage(messages.unknownError) }),
        );
      }
    } finally {
      setIsDownloadInProgress(false);
      setIsDownloadFinished(true);
      setDownloadAbortController(null);
    }
  };

  const menu: ActionBarMenu = {
    kind: 'menu',
    id: 'download',
    text: intl.formatMessage(messages.download),
    items: [
      { id: 'all', text: intl.formatMessage(messages.downloadAll), iconName: 'download' },
      { id: 'predictions', text: intl.formatMessage(messages.downloadPredictions), iconName: 'download' },
      ...(isBaselineAvailable(item)
        ? [{ id: 'baselines', text: intl.formatMessage(messages.downloadBaselines), iconName: 'download' as const }]
        : []),
    ],
    disabled: isDownloadInProgress,
    loading: isDownloadInProgress,
    expandToViewport: true,
    onItemClick: (id) => {
      if (id === 'all') setIsDownloadOptionsOpen(true);
      else if (id === 'predictions' || id === 'baselines') void startDownload(id);
    },
  };

  const dialogs = (
    <>
      <DownloadOptionsModal
        visible={isDownloadOptionsOpen}
        includePageImages={includePageImages}
        includeSourceDocument={includeSourceDocument}
        onIncludePageImagesChange={setIncludePageImages}
        onIncludeSourceDocumentChange={setIncludeSourceDocument}
        onConfirm={() => {
          setIsDownloadOptionsOpen(false);
          void startDownload('all');
        }}
        onDismiss={() => setIsDownloadOptionsOpen(false)}
      />
      <DownloadProgressModal
        visible={(isDownloadInProgress || isDownloadFinished) && downloadScope !== null}
        progress={downloadProgress}
        errors={downloadErrors}
        isFinished={isDownloadFinished}
        onCancel={() => downloadAbortController?.abort()}
        onClose={() => {
          setIsDownloadFinished(false);
          setDownloadProgress(null);
          setDownloadScope(null);
        }}
      />
    </>
  );

  return { menu, dialogs };
};

export default useDocumentDownload;
