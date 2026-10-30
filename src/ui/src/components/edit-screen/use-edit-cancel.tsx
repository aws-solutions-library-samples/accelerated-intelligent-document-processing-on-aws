// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's Cancel, the same in every mode: leave the screen, and when the
// open section has unsaved edits, ask first and discard them. What was already saved
// (a queue item's autosaved draft, a saved section) is kept.
import type React from 'react';
import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

import type { SectionEditorController } from './SectionEditor';
import useDiscardConfirm from './use-discard-confirm';

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
    if (editor.current?.isDirty()) ask({ header: 'Discard your unsaved changes?', confirmLabel: 'Discard and leave', onConfirm: leave });
    else leave();
  }, [ask, editor, leave]);

  return { cancel, dialog };
};

export default useEditCancel;
