// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's editor session: what the page and a mode's model both need to know of
// the open editor (the controller, unsaved and saving state, the open section and its
// working copy), the Edit | View switch, and the running action. Created by the mode
// shell, read by the mode's model and by EditPage.
import type React from 'react';
import { useCallback, useMemo, useRef, useState } from 'react';

import type { EditAction } from './EditScreen';
import type { SectionEditorController, SectionEditorProps } from './SectionEditor';
import useEditView from './use-edit-view';
import type { EditView } from './use-edit-view';

export interface EditorSession {
  controllerRef: React.MutableRefObject<SectionEditorController | null>;
  dirty: boolean;
  saving: boolean;
  sectionData: Record<string, unknown> | null;
  selectedSectionId: string | null;
  /** From useEditView (`?view=source`). */
  view: EditView;
  setView: (v: EditView) => void;
  busy: EditAction | null;
  /**
   * Run an action: `busy` is set while it runs. Resolves true when `fn` succeeded; on a
   * throw, `onError` is told and it resolves false.
   */
  run: (action: EditAction, fn: () => Promise<void>, onError: (err: unknown) => void) => Promise<boolean>;
  editorBindings: Pick<
    SectionEditorProps,
    'controllerRef' | 'onDirtyChange' | 'onSavingChange' | 'onDataChange' | 'onSelectedSectionChange'
  >;
}

const useEditorSession = (): EditorSession => {
  const controllerRef = useRef<SectionEditorController | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [sectionData, setSectionData] = useState<Record<string, unknown> | null>(null);
  const [selectedSectionId, setSelectedSectionId] = useState<string | null>(null);
  const [view, setView] = useEditView();
  const [busy, setBusy] = useState<EditAction | null>(null);

  const run = useCallback(async (action: EditAction, fn: () => Promise<void>, onError: (err: unknown) => void): Promise<boolean> => {
    setBusy(action);
    try {
      await fn();
      return true;
    } catch (err) {
      onError(err);
      return false;
    } finally {
      setBusy(null);
    }
  }, []);

  const editorBindings = useMemo(
    () => ({
      controllerRef,
      onDirtyChange: setDirty,
      onSavingChange: setSaving,
      onDataChange: setSectionData,
      onSelectedSectionChange: setSelectedSectionId,
    }),
    [],
  );

  return { controllerRef, dirty, saving, sectionData, selectedSectionId, view, setView, busy, run, editorBindings };
};

export default useEditorSession;
