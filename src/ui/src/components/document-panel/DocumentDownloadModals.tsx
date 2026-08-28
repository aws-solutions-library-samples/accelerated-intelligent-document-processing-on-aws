// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Alert, Box, Button, Checkbox, Modal, ProgressBar, SpaceBetween } from '@cloudscape-design/components';
import type { ExportErrorEntry, ExportProgress, ExportScope } from './document-export';

/**
 * Selecting more documents than this triggers a size/time warning: the archive is
 * assembled in browser memory, so very large bulk exports can exhaust the tab.
 */
export const LARGE_SELECTION_THRESHOLD = 25;

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

const SCOPE_DESCRIPTIONS: Record<ExportScope, React.ReactNode> = {
  all: (
    <>
      Packages the document summary, section predictions, baselines (when available), per-page text, and confidence into a single ZIP
      archive.
    </>
  ),
  predictions: <>Packages the extracted section predictions (the model&apos;s output JSON) into a single ZIP archive.</>,
  baselines: <>Packages the evaluation baseline (ground truth) JSON into a single ZIP archive.</>,
};

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
  const isBulk = (documentCount ?? 1) > 1;
  const header = isBulk ? `Download data for ${documentCount} documents` : 'Download all document data';

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={header}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" onClick={onConfirm}>
              Start download
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          {SCOPE_DESCRIPTIONS[scope]} Folder structure mirrors the S3 buckets (<code>output/</code>, <code>baseline/</code>,{' '}
          <code>input/</code>)
          {isBulk && (
            <>
              , with one top-level folder per document and a root <code>manifest.json</code> index
            </>
          )}
          .
        </Box>
        {isBulk && (documentCount ?? 0) > LARGE_SELECTION_THRESHOLD && (
          <Alert type="warning" header={`${documentCount} documents selected`}>
            Large exports are assembled in browser memory and may take several minutes. Consider exporting in smaller batches, and leave
            page images out unless you need them.
          </Alert>
        )}
        {scope === 'all' && (
          <>
            <Checkbox checked={includePageImages} onChange={({ detail }) => onIncludePageImagesChange(detail.checked)}>
              Include page images (can significantly increase archive size)
            </Checkbox>
            <Checkbox checked={includeSourceDocument} onChange={({ detail }) => onIncludeSourceDocumentChange(detail.checked)}>
              Include source document (original uploaded file from the input bucket)
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
  const total = progress?.total ?? 1;
  const completed = progress?.completed ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  const documentsTotal = progress?.documentsTotal ?? 1;
  let description: string;
  if (progress?.phase === 'preparing') {
    // Hydration phase: the counts are documents, not files.
    description = `${completed} of ${total} document${total === 1 ? '' : 's'} loaded`;
  } else {
    const fileProgress = `${completed} of ${total} files processed`;
    description =
      documentsTotal > 1 ? `${fileProgress} · ${progress?.documentsCompleted ?? 0} of ${documentsTotal} documents` : fileProgress;
  }

  return (
    <Modal
      visible={visible}
      onDismiss={isFinished ? onClose : undefined}
      header={isFinished ? 'Download complete' : 'Preparing download'}
      footer={
        <Box float="right">
          {isFinished ? (
            <Button variant="primary" onClick={onClose}>
              Close
            </Button>
          ) : (
            <Button variant="link" onClick={onCancel} disabled={isCancelling}>
              {isCancelling ? 'Cancelling…' : 'Cancel'}
            </Button>
          )}
        </Box>
      }
    >
      <SpaceBetween size="s">
        <ProgressBar value={pct} additionalInfo={progress?.currentFile ?? ''} description={description} label="Export progress" />
        {errors.length > 0 && (
          <Alert type="warning" header={`${errors.length} item(s) could not be included`}>
            <Box>
              These entries were skipped and recorded in the archive&apos;s <code>manifest.json</code>:
            </Box>
            <ul style={{ marginTop: '8px', maxHeight: '160px', overflow: 'auto' }}>
              {errors.slice(0, 25).map((e) => (
                <li key={`${e.document ?? ''}-${e.path}-${e.message}`}>
                  <code>{e.document ? `${e.document} → ${e.path}` : e.path}</code>: {e.message}
                </li>
              ))}
              {errors.length > 25 && <li>…and {errors.length - 25} more</li>}
            </ul>
          </Alert>
        )}
      </SpaceBetween>
    </Modal>
  );
};
