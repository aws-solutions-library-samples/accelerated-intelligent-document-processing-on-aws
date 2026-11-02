// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's "discard your unsaved changes?" question, for an action that would
// lose them (Cancel, Refresh): Keep editing, or the action's own confirm button. What was
// already saved is kept.
import React, { useCallback, useState } from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

export const DISCARD_BODY = 'The changes you made since the last save are lost. Anything already saved is kept.';

interface DiscardQuestion {
  header: string;
  confirmLabel: string;
  onConfirm: () => void;
}

interface DiscardConfirm {
  /** Ask; `onConfirm` runs when the changes are to be discarded. */
  ask: (question: DiscardQuestion) => void;
  /** The confirmation dialog; render it with the screen's dialogs. */
  dialog: React.ReactNode;
}

const useDiscardConfirm = (): DiscardConfirm => {
  const [question, setQuestion] = useState<DiscardQuestion | null>(null);
  const ask = useCallback((q: DiscardQuestion) => setQuestion(q), []);

  const dialog = question && (
    <Modal
      visible
      header={question.header}
      onDismiss={() => setQuestion(null)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => setQuestion(null)}>
              Keep editing
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                setQuestion(null);
                question.onConfirm();
              }}
            >
              {question.confirmLabel}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      {DISCARD_BODY}
    </Modal>
  );

  return { ask, dialog };
};

export default useDiscardConfirm;
