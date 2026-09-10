// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * RemoveDocumentsModal — confirm dropping the selected documents from a test set's
 * working draft. Names what is deleted (the file and every label it carries),
 * what is not (published versions), and says when the set will be left empty.
 */

import React from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import type { TestSetDocumentItem } from './TestSetDetail';

/** Names shown in full before the list collapses to a count. */
const MAX_LISTED = 10;

interface RemoveDocumentsModalProps {
  visible: boolean;
  documents: TestSetDocumentItem[];
  /** Documents the set will have once these are gone. */
  remaining: number;
  submitting: boolean;
  onDismiss: () => void;
  onConfirm: () => void;
}

const RemoveDocumentsModal = ({
  visible,
  documents,
  remaining,
  submitting,
  onDismiss,
  onConfirm,
}: RemoveDocumentsModalProps): React.JSX.Element => {
  const count = documents.length;
  const reviewed = documents.filter((d) => d.labelSource === 'reviewed-human').length;
  const listed = documents.slice(0, MAX_LISTED);
  const unlisted = count - listed.length;
  const noun = count === 1 ? 'document' : 'documents';

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={`Remove ${count} ${noun}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" iconName="remove" onClick={onConfirm} loading={submitting} disabled={count === 0}>
              Remove {count} {noun}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          Removes {count === 1 ? 'this document' : `these ${count} documents`} from the test set: the file and every label it carries are
          deleted. Versions you have already published are not affected.
        </Box>
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {listed.map((d) => (
            <li key={d.inputKey}>{d.objectKey}</li>
          ))}
          {unlisted > 0 && (
            <li>
              <Box variant="span" color="text-body-secondary">
                + {unlisted} more
              </Box>
            </li>
          )}
        </ul>
        {reviewed > 0 && (
          <Alert type="warning">
            {reviewed} of these {reviewed === 1 ? 'carries' : 'carry'} reviewed labels. That review work cannot be recovered.
          </Alert>
        )}
        {count > 0 && remaining === 0 && (
          <Alert type="info">This removes every document. The set stays, empty, and you can add documents to it later.</Alert>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default RemoveDocumentsModal;
