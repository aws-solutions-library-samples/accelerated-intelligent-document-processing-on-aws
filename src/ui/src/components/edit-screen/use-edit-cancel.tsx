// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's Cancel, the same in every mode: leave the screen, and when the
// open section has unsaved edits, ask first and discard them. What was already saved
// (a queue item's autosaved draft, a saved section) is kept.
import React, { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

import type { SectionEditorController } from './SectionEditor';

interface EditCancel {
  /** Run Cancel: leave now, or ask first when there are unsaved edits. */
  cancel: () => void;
  /** The confirmation dialog; render it with the screen's dialogs. */
  dialog: React.ReactNode;
}

const useEditCancel = (editor: React.RefObject<SectionEditorController | null>, leaveTo: string): EditCancel => {
  const navigate = useNavigate();
  const [asking, setAsking] = useState(false);

  const leave = useCallback(() => {
    editor.current?.discard();
    setAsking(false);
    navigate(leaveTo);
  }, [editor, leaveTo, navigate]);

  const cancel = useCallback(() => {
    if (editor.current?.isDirty()) setAsking(true);
    else leave();
  }, [editor, leave]);

  const dialog = asking && (
    <Modal
      visible
      header="Discard your unsaved changes?"
      onDismiss={() => setAsking(false)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => setAsking(false)}>
              Keep editing
            </Button>
            <Button variant="primary" onClick={leave}>
              Discard and leave
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      The changes you made since the last save are lost. Anything already saved is kept.
    </Modal>
  );

  return { cancel, dialog };
};

export default useEditCancel;
