// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The "Confirm Reprocessing" dialog before processChanges: what reprocessing will do,
// with section structure changes or without them. Shared by the document page's
// Sections panel and the unified editor's Process Changes on a direct edit.
import React from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

interface ProcessChangesConfirmModalProps {
  visible: boolean;
  /** Sections were added, removed, reclassified or regrouped. */
  structuralChanges: boolean;
  /** BDA mode, whose processing is skipped rather than the Pipeline mode steps. */
  bdaMode?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

const ProcessChangesConfirmModal = ({
  visible,
  structuralChanges,
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
            Confirm & Process
          </Button>
        </SpaceBetween>
      </Box>
    }
    header="Confirm Reprocessing"
  >
    <SpaceBetween size="s">
      {structuralChanges ? (
        <>
          <Box>You are about to save changes to document sections and trigger selective reprocessing. This will:</Box>
          <ul>
            <li>Update section classifications and page assignments</li>
            <li>Remove extraction data for modified sections</li>
            <li>Reprocess only the changed sections (skipping OCR and classification steps)</li>
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
