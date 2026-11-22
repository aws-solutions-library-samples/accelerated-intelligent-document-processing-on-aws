// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's Cancel, the same in every mode: leave the screen, and when the
// open section has unsaved edits, ask first and discard them. What was already saved
// (a queue item's autosaved draft, a saved section) is kept.
import type React from 'react';
import { useCallback } from 'react';
import { defineMessages, type NoMessageValues } from 'react-intl';
import { useNavigate } from 'react-router-dom';

import type { SectionEditorController } from './SectionEditor';
import useDiscardConfirm from './use-discard-confirm';

const messages = defineMessages<{ header: NoMessageValues; confirm: NoMessageValues }>({
  header: {
    id: 'editScreen.discard.cancel.header',
    defaultMessage: 'Discard your unsaved changes?',
    description: 'Title of the dialog the edit page shows when Cancel would lose unsaved changes.',
  },
  confirm: {
    id: 'editScreen.discard.cancel.confirm',
    defaultMessage: 'Discard and leave',
    description: 'Confirm button of the dialog that asks before Cancel loses unsaved changes: it discards them and leaves the edit page.',
  },
});

interface EditCancel {
  /** Run Cancel: leave now, or ask first when there are unsaved edits. */
  cancel: () => void;
  /** The confirmation dialog; render it with the screen's dialogs. */
  dialog: React.ReactNode;
}

const useEditCancel = (editor: React.RefObject<SectionEditorController | null>, leaveTo: string): EditCancel => {
  const navigate = useNavigate();
  const { ask, dialog } = useDiscardConfirm();

  const leave = useCallback(() => {
    editor.current?.discard();
    navigate(leaveTo);
  }, [editor, leaveTo, navigate]);

  const cancel = useCallback(() => {
    if (editor.current?.isDirty()) ask({ header: messages.header, confirmLabel: messages.confirm, onConfirm: leave });
    else leave();
  }, [ask, editor, leave]);

  return { cancel, dialog };
};

export default useEditCancel;
