// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Edit Page Class, the same on the edit page and the document page: the page-class board
// as a dialog, over the board's pages and sections, with its save error shown inside the
// board so the work in it is not lost. The board decides what saving does and what it
// says.
//
// A board that can re-extract (the document page's) also offers Save and Re-extract:
// after a confirmation listing the sections whose class or pages changed, it saves the
// grouping, then extracts those sections again. When the save works but the re-extract
// fails, the board closes (the grouping it shows is saved) and an "Edit Page Class"
// dialog reports the failure, with Re-extract again.
import React, { useMemo, useState } from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import usePageThumbnails, { THUMBNAIL_FAILURE_MESSAGES } from '../../hooks/use-page-thumbnails';
import { getErrorMessage } from '../../utils/errorUtils';
import PageGroupingEditor from '../common/PageGroupingEditor';
import ProcessChangesConfirmModal from '../common/ProcessChangesConfirmModal';
import { changedSections } from '../common/section-grouping';
import type { GroupedSection } from '../common/section-grouping';
import type { EditBoard } from './edit-model';

/** A section's line in the re-extract lists. */
const sectionLine = (s: GroupedSection): string => `Section ${s.sectionId}: ${s.documentClass ?? ''}, pages ${s.pageIds.join(', ')}`;

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
  // Save and Re-extract: the grouping waiting for its confirmation, and a re-extract that failed after the save.
  const [confirm, setConfirm] = useState<{ next: GroupedSection[]; changed: GroupedSection[] } | null>(null);
  const [partial, setPartial] = useState<{ changed: GroupedSection[]; message: string } | null>(null);
  const [isRetrying, setIsRetrying] = useState(false);
  const thumbnailPages = board?.pages.kind === 'thumbnails' ? board.pages.pages : undefined;
  // Thumbnails are fetched only while the board is open.
  const { urls: thumbnailUrls, failure: thumbnailFailure } = usePageThumbnails(thumbnailPages, { enabled: isOpen && !!thumbnailPages });
  const original = useMemo(() => (board && isOpen ? board.sections(openSectionId, openData) : []), [board, isOpen]);

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

  const reextract = async (changed: GroupedSection[]) => {
    if (!board?.reextract) return;
    try {
      const message = await board.reextract.run(changed);
      setPartial(null);
      setSuccessMessage(message);
    } catch (err) {
      setPartial({ changed, message: getErrorMessage(err) });
    }
  };

  const handleSaveAndReextract = async () => {
    if (!board?.reextract || !confirm) return;
    const { next, changed } = confirm;
    setIsSaving(true);
    setError(null);
    try {
      await board.save(next);
    } catch (err) {
      // The grouping was not saved: the board stays open with the reason.
      setError(getErrorMessage(err));
      setConfirm(null);
      setIsSaving(false);
      return;
    }
    setConfirm(null);
    setIsOpen(false);
    setIsSaving(false);
    await reextract(changed);
  };

  const retry = async () => {
    if (!partial) return;
    setIsRetrying(true);
    try {
      await reextract(partial.changed);
    } finally {
      setIsRetrying(false);
    }
  };

  const open = () => {
    setError(null);
    setIsOpen(true);
  };

  const { reextract: reextractBoard } = board ?? {};
  const boardDialog = board && isOpen && (
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
          {thumbnailFailure && (
            <Alert type={thumbnailFailure === 'access-denied' ? 'warning' : 'error'} header="Page thumbnails unavailable">
              {THUMBNAIL_FAILURE_MESSAGES[thumbnailFailure]}
            </Alert>
          )}
          {board.consequence}
        </>
      }
      saveLabel="Save page grouping"
      secondarySave={
        reextractBoard && {
          label: 'Save and Re-extract',
          disabledReason: (draft) => reextractBoard.disabledReason(changedSections(original, draft)),
          onSave: (next) => setConfirm({ next, changed: changedSections(original, next) }),
        }
      }
      isSaving={isSaving}
      onSave={handleSave}
      onCancel={() => setIsOpen(false)}
    />
  );

  const dialog = (
    <>
      {boardDialog}
      {confirm && (
        <ProcessChangesConfirmModal
          visible
          reextractSections={confirm.changed}
          busy={isSaving}
          onConfirm={() => void handleSaveAndReextract()}
          onCancel={() => setConfirm(null)}
        />
      )}
      {partial && (
        <Modal
          visible
          header="Edit Page Class"
          onDismiss={() => setPartial(null)}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setPartial(null)}>Close</Button>
                <Button variant="primary" loading={isRetrying} onClick={() => void retry()}>
                  Re-extract again
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="s">
            <Alert type="error">The page grouping was saved, but the sections were not re-extracted: {partial.message}</Alert>
            <ul>
              {partial.changed.map((s) => (
                <li key={s.sectionId}>{sectionLine(s)}</li>
              ))}
            </ul>
          </SpaceBetween>
        </Modal>
      )}
    </>
  );

  return { open, dialog };
};

export default usePageClassBoard;
