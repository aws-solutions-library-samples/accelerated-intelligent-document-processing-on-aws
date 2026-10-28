// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Edit Page Class, the same in every mode: the page-class board as a dialog, over the
// model's pages and sections, with its save error shown inside the board so the work
// in it is not lost. The model decides what saving does and what it says.
import React, { useMemo, useState } from 'react';
import { Alert } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import usePageThumbnails from '../../hooks/use-page-thumbnails';
import { getErrorMessage } from '../../utils/errorUtils';
import PageGroupingEditor from '../common/PageGroupingEditor';
import type { GroupedSection } from '../common/section-grouping';
import type { EditBoard } from './edit-model';

export interface PageClassBoard {
  open: () => void;
  /** The dialog while it is open; render it with the page's dialogs. */
  dialog: React.ReactNode;
}

const usePageClassBoard = (
  board: EditBoard | null,
  openSectionId: string | null,
  openData: Record<string, unknown> | null,
): PageClassBoard => {
  const { setSuccessMessage } = useAppContext();
  const [isOpen, setIsOpen] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const thumbnailPages = board?.pages.kind === 'thumbnails' ? board.pages.pages : undefined;
  // Thumbnails are fetched only while the board is open.
  const { urls: thumbnailUrls } = usePageThumbnails(thumbnailPages, { enabled: isOpen && !!thumbnailPages });

  const boardPages = useMemo(() => {
    if (!board) return [];
    if (board.pages.kind === 'rendered') return board.pages.pages;
    return board.pages.pages.map((page) => ({ id: Number(page.Id), imageUri: thumbnailUrls[String(page.Id)] ?? null }));
  }, [board, thumbnailUrls]);

  const handleSave = async (next: GroupedSection[]) => {
    if (!board) return;
    setIsSaving(true);
    setError(null);
    try {
      const message = await board.save(next);
      setIsOpen(false);
      if (message) setSuccessMessage(message);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setIsSaving(false);
    }
  };

  const open = () => {
    setError(null);
    setIsOpen(true);
  };

  const dialog = board && isOpen && (
    <PageGroupingEditor
      presentation="modal"
      title="Edit Page Class"
      pages={boardPages}
      sections={board.sections(openSectionId, openData)}
      classOptions={board.classOptions}
      canChangeClass={board.canChangeClass}
      consequence={
        <>
          {error && <Alert type="error">{error}</Alert>}
          {board.consequence}
        </>
      }
      saveLabel="Save page grouping"
      isSaving={isSaving}
      onSave={handleSave}
      onCancel={() => setIsOpen(false)}
    />
  );

  return { open, dialog };
};

export default usePageClassBoard;
