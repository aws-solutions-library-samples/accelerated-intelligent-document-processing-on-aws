// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Alert, Box, Button, Checkbox, Modal, ProgressBar, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';
import type { ExportErrorEntry, ExportProgress, ExportScope } from './document-export';

/**
 * Selecting more documents than this triggers a size/time warning: the archive is
 * assembled in browser memory, so very large bulk exports can exhaust the tab.
 */
export const LARGE_SELECTION_THRESHOLD = 25;

/** What each export scope packages: the first sentence of the options dialog. */
const scopeMessages = defineMessages<Record<ExportScope, NoMessageValues>>({
  all: {
    id: 'documents.downloadOptions.scope.all',
    defaultMessage:
      'Packages the document summary, section predictions, baselines (when available), per-page text, and confidence into a single ZIP archive.',
    description: 'What the Download All export contains, in the download options dialog.',
  },
  predictions: {
    id: 'documents.downloadOptions.scope.predictions',
    defaultMessage: "Packages the extracted section predictions (the model's output JSON) into a single ZIP archive.",
    description: 'What the Download Predictions export contains, in the download options dialog.',
  },
  baselines: {
    id: 'documents.downloadOptions.scope.baselines',
    defaultMessage: 'Packages the evaluation baseline (ground truth) JSON into a single ZIP archive.',
    description: 'What the Download Baselines export contains, in the download options dialog.',
  },
});

const optionsMessages = defineMessages<{
  title: NoMessageValues;
  titleBulk: { readonly count: number };
  cancel: NoMessageValues;
  start: NoMessageValues;
  layout: { readonly scope: string; readonly code: MessageTag };
  layoutBulk: { readonly scope: string; readonly code: MessageTag };
  largeTitle: { readonly count: number };
  largeBody: NoMessageValues;
  includePageImages: NoMessageValues;
  includeSource: NoMessageValues;
}>({
  title: {
    id: 'documents.downloadOptions.title',
    defaultMessage: 'Download all document data',
    description: 'Title of the download options dialog for one document.',
  },
  titleBulk: {
    id: 'documents.downloadOptions.titleBulk',
    defaultMessage: 'Download data for {count, plural, one {# document} other {# documents}}',
    description: 'Title of the download options dialog for several documents. {count} is the number of selected documents (2 or more).',
  },
  cancel: {
    id: 'documents.downloadOptions.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the download options dialog without downloading.',
  },
  start: {
    id: 'documents.downloadOptions.start',
    defaultMessage: 'Start download',
    description: 'Button of the download options dialog that starts building the ZIP archive.',
  },
  layout: {
    id: 'documents.downloadOptions.layout',
    defaultMessage: '{scope} Folder structure mirrors the S3 buckets (<code>output/</code>, <code>baseline/</code>, <code>input/</code>).',
    description:
      'Describes a one-document ZIP archive. {scope} is the sentence saying what the export contains; the folder names in <code> stay as written.',
  },
  layoutBulk: {
    id: 'documents.downloadOptions.layoutBulk',
    defaultMessage:
      '{scope} Folder structure mirrors the S3 buckets (<code>output/</code>, <code>baseline/</code>, <code>input/</code>), with one top-level folder per document and a root <code>manifest.json</code> index.',
    description:
      'Describes a several-document ZIP archive. {scope} is the sentence saying what the export contains; the folder and file names in <code> stay as written.',
  },
  largeTitle: {
    id: 'documents.downloadOptions.largeTitle',
    defaultMessage: '{count, plural, one {# document selected} other {# documents selected}}',
    description: 'Heading of the warning about a large export. {count} is the number of selected documents (more than 25).',
  },
  largeBody: {
    id: 'documents.downloadOptions.largeBody',
    defaultMessage:
      'Large exports are assembled in browser memory and may take several minutes. Consider exporting in smaller batches, and leave page images out unless you need them.',
    description: 'Body of the warning about a large export in the download options dialog.',
  },
  includePageImages: {
    id: 'documents.downloadOptions.includePageImages',
    defaultMessage: 'Include page images (can significantly increase archive size)',
    description: 'Checkbox in the download options dialog that adds the page images to the archive.',
  },
  includeSource: {
    id: 'documents.downloadOptions.includeSource',
    defaultMessage: 'Include source document (original uploaded file from the input bucket)',
    description: 'Checkbox in the download options dialog that adds the uploaded file to the archive.',
  },
});

const progressMessages = defineMessages<{
  loaded: { readonly completed: number; readonly total: number };
  files: { readonly completed: number; readonly total: number };
  filesAndDocuments: {
    readonly completed: number;
    readonly total: number;
    readonly documentsCompleted: number;
    readonly documentsTotal: number;
  };
  done: NoMessageValues;
  preparing: NoMessageValues;
  close: NoMessageValues;
  cancelling: NoMessageValues;
  cancel: NoMessageValues;
  label: NoMessageValues;
  errorsTitle: { readonly count: number };
  errorsBody: { readonly code: MessageTag };
  more: { readonly count: number };
}>({
  loaded: {
    id: 'documents.downloadProgress.loaded',
    defaultMessage: '{completed, number} of {total, plural, one {# document} other {# documents}} loaded',
    description: 'Progress of loading the selected documents before a bulk export. {completed} and {total} count documents.',
  },
  files: {
    id: 'documents.downloadProgress.files',
    defaultMessage: '{completed, number} of {total, plural, one {# file} other {# files}} processed',
    description: 'Progress of a one-document export. {completed} and {total} count the files (and the final archive step).',
  },
  filesAndDocuments: {
    id: 'documents.downloadProgress.filesAndDocuments',
    defaultMessage:
      '{completed, number} of {total, plural, one {# file} other {# files}} processed · {documentsCompleted, number} of {documentsTotal, plural, one {# document} other {# documents}}',
    description:
      'Progress of a several-document export. {completed} and {total} count files; {documentsCompleted} and {documentsTotal} count documents.',
  },
  done: {
    id: 'documents.downloadProgress.done',
    defaultMessage: 'Download complete',
    description: 'Title of the download progress dialog once the archive is ready.',
  },
  preparing: {
    id: 'documents.downloadProgress.preparing',
    defaultMessage: 'Preparing download',
    description: 'Title of the download progress dialog while the archive is being built.',
  },
  close: {
    id: 'documents.downloadProgress.close',
    defaultMessage: 'Close',
    description: 'Button that closes the download progress dialog once the download is complete.',
  },
  cancelling: {
    id: 'documents.downloadProgress.cancelling',
    defaultMessage: 'Cancelling…',
    description: 'Cancel button of the download progress dialog once cancelling is requested.',
  },
  cancel: {
    id: 'documents.downloadProgress.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the download progress dialog that stops the export.',
  },
  label: {
    id: 'documents.downloadProgress.label',
    defaultMessage: 'Export progress',
    description: 'Label of the progress bar in the download progress dialog.',
  },
  errorsTitle: {
    id: 'documents.downloadProgress.errorsTitle',
    defaultMessage: '{count, plural, one {# item} other {# items}} could not be included',
    description: 'Heading of the list of files left out of the archive. {count} is the number of files.',
  },
  errorsBody: {
    id: 'documents.downloadProgress.errorsBody',
    defaultMessage: "These entries were skipped and recorded in the archive's <code>manifest.json</code>:",
    description: 'Introduces the list of files left out of the archive; the file name in <code> stays as written.',
  },
  more: {
    id: 'documents.downloadProgress.more',
    defaultMessage: '…and {count, number} more',
    description: 'Last line of the list of files left out of the archive. {count} is the number of entries not listed.',
  },
});

/** The line under the progress bar while the export is at a stage with no file to show. */
const stageMessages = defineMessages<{
  preparing: NoMessageValues;
  generating: NoMessageValues;
  generatingPercent: { readonly ratio: number };
  done: NoMessageValues;
}>({
  preparing: {
    id: 'documents.downloadProgress.stage.preparing',
    defaultMessage: 'Preparing download…',
    description: 'Shown under the export progress bar while the download links are being prepared.',
  },
  generating: {
    id: 'documents.downloadProgress.stage.generating',
    defaultMessage: 'Generating archive…',
    description: 'Shown under the export progress bar when the ZIP archive starts being built.',
  },
  generatingPercent: {
    id: 'documents.downloadProgress.stage.generatingPercent',
    defaultMessage: 'Generating archive… {ratio, number, ::percent}',
    description: 'Shown under the export progress bar while the ZIP archive is built. {ratio} is how far it is (0 to 1).',
  },
  done: {
    id: 'documents.downloadProgress.stage.done',
    defaultMessage: 'Done',
    description: 'Shown under the export progress bar once the archive is ready.',
  },
});

/** Renders a `<code>...</code>` element value: a folder or file name of the archive. */
const code = (chunks: React.ReactNode[]): React.JSX.Element => <code>{chunks}</code>;

interface DownloadOptionsModalProps {
  visible: boolean;
  includePageImages: boolean;
  includeSourceDocument: boolean;
  onIncludePageImagesChange: (value: boolean) => void;
  onIncludeSourceDocumentChange: (value: boolean) => void;
  onConfirm: () => void;
  onDismiss: () => void;
  /** Scope being confirmed. Optional-asset toggles only apply to `all`. */
  scope?: ExportScope;
  /** Number of documents in the export; omit (or 1) for a single-document export. */
  documentCount?: number;
}

/**
 * Confirmation shown before a ZIP export: toggles heavy optional assets for the
 * "all" scope, and for bulk exports states how many documents are included.
 */
export const DownloadOptionsModal = ({
  visible,
  includePageImages,
  includeSourceDocument,
  onIncludePageImagesChange,
  onIncludeSourceDocumentChange,
  onConfirm,
  onDismiss,
  scope = 'all',
  documentCount,
}: DownloadOptionsModalProps): React.JSX.Element => {
  const intl = useIntl();
  const count = documentCount ?? 1;
  const isBulk = count > 1;
  const header = isBulk ? intl.formatMessage(optionsMessages.titleBulk, { count }) : intl.formatMessage(optionsMessages.title);

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={header}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              <FormattedMessage {...optionsMessages.cancel} />
            </Button>
            <Button variant="primary" onClick={onConfirm}>
              <FormattedMessage {...optionsMessages.start} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          <FormattedMessage
            {...(isBulk ? optionsMessages.layoutBulk : optionsMessages.layout)}
            values={{ scope: intl.formatMessage(scopeMessages[scope]), code }}
          />
        </Box>
        {isBulk && count > LARGE_SELECTION_THRESHOLD && (
          <Alert type="warning" header={intl.formatMessage(optionsMessages.largeTitle, { count })}>
            <FormattedMessage {...optionsMessages.largeBody} />
          </Alert>
        )}
        {scope === 'all' && (
          <>
            <Checkbox checked={includePageImages} onChange={({ detail }) => onIncludePageImagesChange(detail.checked)}>
              <FormattedMessage {...optionsMessages.includePageImages} />
            </Checkbox>
            <Checkbox checked={includeSourceDocument} onChange={({ detail }) => onIncludeSourceDocumentChange(detail.checked)}>
              <FormattedMessage {...optionsMessages.includeSource} />
            </Checkbox>
          </>
        )}
      </SpaceBetween>
    </Modal>
  );
};

interface DownloadProgressModalProps {
  visible: boolean;
  progress: ExportProgress | null;
  errors: ExportErrorEntry[];
  isFinished: boolean;
  onCancel: () => void;
  onClose: () => void;
  /** True once cancellation is requested but the current step is still finishing. */
  isCancelling?: boolean;
}

/** Long-running progress + error summary modal shown during exports. */
export const DownloadProgressModal = ({
  visible,
  progress,
  errors,
  isFinished,
  onCancel,
  onClose,
  isCancelling = false,
}: DownloadProgressModalProps): React.JSX.Element => {
  const intl = useIntl();
  const total = progress?.total ?? 1;
  const completed = progress?.completed ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  const documentsTotal = progress?.documentsTotal ?? 1;
  let description: string;
  if (progress?.phase === 'preparing') {
    // Hydration phase: the counts are documents, not files.
    description = intl.formatMessage(progressMessages.loaded, { completed, total });
  } else if (documentsTotal > 1) {
    description = intl.formatMessage(progressMessages.filesAndDocuments, {
      completed,
      total,
      documentsCompleted: progress?.documentsCompleted ?? 0,
      documentsTotal,
    });
  } else {
    description = intl.formatMessage(progressMessages.files, { completed, total });
  }

  // The export's stage when it reports one, else the file being processed.
  let additionalInfo = progress?.currentFile ?? '';
  if (progress?.stage === 'generating' && progress.percent !== undefined) {
    // Rounded first, as the percentage always was: `::percent` alone would round the ratio's decimal form.
    additionalInfo = intl.formatMessage(stageMessages.generatingPercent, { ratio: Math.round(progress.percent) / 100 });
  } else if (progress?.stage) {
    additionalInfo = intl.formatMessage(stageMessages[progress.stage]);
  }

  return (
    <Modal
      visible={visible}
      onDismiss={isFinished ? onClose : undefined}
      header={intl.formatMessage(isFinished ? progressMessages.done : progressMessages.preparing)}
      footer={
        <Box float="right">
          {isFinished ? (
            <Button variant="primary" onClick={onClose}>
              <FormattedMessage {...progressMessages.close} />
            </Button>
          ) : (
            <Button variant="link" onClick={onCancel} disabled={isCancelling}>
              <FormattedMessage {...(isCancelling ? progressMessages.cancelling : progressMessages.cancel)} />
            </Button>
          )}
        </Box>
      }
    >
      <SpaceBetween size="s">
        <ProgressBar
          value={pct}
          additionalInfo={additionalInfo}
          description={description}
          label={intl.formatMessage(progressMessages.label)}
        />
        {errors.length > 0 && (
          <Alert type="warning" header={intl.formatMessage(progressMessages.errorsTitle, { count: errors.length })}>
            <Box>
              <FormattedMessage {...progressMessages.errorsBody} values={{ code }} />
            </Box>
            <ul style={{ marginTop: '8px', maxHeight: '160px', overflow: 'auto' }}>
              {errors.slice(0, 25).map((e) => (
                <li key={`${e.document ?? ''}-${e.path}-${e.message}`}>
                  <code>{e.document ? `${e.document} → ${e.path}` : e.path}</code>: {e.message}
                </li>
              ))}
              {errors.length > 25 && (
                <li>
                  <FormattedMessage {...progressMessages.more} values={{ count: errors.length - 25 }} />
                </li>
              )}
            </ul>
          </Alert>
        )}
      </SpaceBetween>
    </Modal>
  );
};
