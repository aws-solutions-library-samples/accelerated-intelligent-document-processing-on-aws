// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The confirmation before Process Changes. The 'reprocess' variant ("Confirm
// Reprocessing") says what reprocessing will do: shared by the edit page's Process Changes
// on a direct edit and the document page's Edit Page Grouping › Save and Re-extract, which
// lists the sections it re-extracts. The 'version' variant is Test Studio's Process Changes
// while no draft version of the set is open: it opens one and writes reviewed ground truth.
import React from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

import { PROCESS_CHANGES_LABEL } from './labels';

export const VERSION_CONFIRM_HEADER = 'Open a new version and save ground truth';
export const VERSION_CONFIRM_BODY = `${PROCESS_CHANGES_LABEL} opens the next version of this test set (the current version stays as it is) and writes your labels as reviewed ground truth.`;

interface ProcessChangesConfirmModalProps {
  visible: boolean;
  /** What is confirmed: reprocessing the document (default), or opening the test set's next version. */
  variant?: 'reprocess' | 'version';
  /** Save and Re-extract: the sections saved, then extracted again. */
  reextractSections?: { sectionId: string; documentClass?: string | null; pageIds: number[] }[];
  /** BDA mode, whose processing is skipped rather than the Pipeline mode steps. */
  bdaMode?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

const ProcessChangesConfirmModal = ({
  visible,
  variant = 'reprocess',
  reextractSections,
  bdaMode = false,
  busy = false,
  onConfirm,
  onCancel,
}: ProcessChangesConfirmModalProps): React.JSX.Element => (
  <Modal
    onDismiss={onCancel}
    visible={visible}
    footer={
      <Box float="right">
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={onConfirm}>
            {variant === 'version' ? PROCESS_CHANGES_LABEL : 'Confirm & Process'}
          </Button>
        </SpaceBetween>
      </Box>
    }
    header={variant === 'version' ? VERSION_CONFIRM_HEADER : 'Confirm Reprocessing'}
  >
    <SpaceBetween size="s">
      {variant === 'version' ? (
        <Box>{VERSION_CONFIRM_BODY}</Box>
      ) : reextractSections ? (
        <>
          <Box>You are about to save the new page grouping, then extract these sections again:</Box>
          <ul>
            {reextractSections.map((s) => (
              <li key={s.sectionId}>
                Section {s.sectionId}: {s.documentClass}, pages {s.pageIds.join(', ')}
              </li>
            ))}
          </ul>
          <Box>This will:</Box>
          <ul>
            <li>Replace their extracted values, including any corrected by hand</li>
            <li>Record their classes as confirmed (class confidence 1.0)</li>
            <li>Mark this document&apos;s pending review as completed, if it has one</li>
            <li>Clear the document&apos;s rule validation results</li>
            <li>Leave the other sections&apos; values as they are; OCR and classification are not repeated</li>
          </ul>
        </>
      ) : (
        <>
          <Box>
            No section structure changes detected. This will trigger <strong>evaluation and summarization</strong> reprocessing.
          </Box>
          <Box>Use this when you have edited extraction data (predictions or baseline) and want to:</Box>
          <ul>
            <li>Re-run evaluation to compare predictions against baseline</li>
            <li>Update the document summary report</li>
          </ul>
          <Alert type="info">
            {bdaMode
              ? 'BDA (Bedrock Data Automation) processing will be automatically skipped since existing data is preserved.'
              : 'OCR, Classification, Extraction, and Assessment steps will be automatically skipped since existing data is preserved.'}
          </Alert>
        </>
      )}
    </SpaceBetween>
  </Modal>
);

export default ProcessChangesConfirmModal;
