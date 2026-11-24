// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Edit Page Grouping, the same on the edit page and the document page: the page-class board
// as a dialog, over the board's pages and sections, with its save error shown inside the
// board so the work in it is not lost. The board decides what saving does and what it
// says.
//
// A board that can re-extract (the document page's) also offers Save and Re-extract:
// after a confirmation listing the sections whose class or pages changed, it saves the
// grouping, then extracts those sections again. When the save works but the re-extract
// fails, the board closes (the grouping it shows is saved) and an "Edit Page Grouping"
// dialog reports the failure, with Re-extract again.
import React, { useMemo, useState } from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

import useAppContext from '../../contexts/app';
import usePageThumbnails, { THUMBNAIL_FAILURE_MESSAGES } from '../../hooks/use-page-thumbnails';
import { getErrorMessage } from '../../utils/errorUtils';
import PageGroupingEditor from '../common/PageGroupingEditor';
import ProcessChangesConfirmModal from '../common/ProcessChangesConfirmModal';
import { changedSections } from '../common/section-grouping';
import type { GroupedSection } from '../common/section-grouping';
import type { EditBoard } from './edit-model';

const messages = defineMessages<{
  title: NoMessageValues;
  thumbnailsUnavailable: NoMessageValues;
  save: NoMessageValues;
  saveAndReextract: NoMessageValues;
  close: NoMessageValues;
  reextractAgain: NoMessageValues;
  partialFailed: { readonly message: string };
  sectionLine: { readonly count: number; readonly id: string; readonly class: string; readonly pages: string };
}>({
  title: {
    id: 'editScreen.pageGrouping.title',
    defaultMessage: 'Edit Page Grouping',
    description:
      "Title of the Edit Page Grouping dialog, on the edit page and the document page, where the document's pages are regrouped into sections; also the title of the dialog that reports a re-extract that failed after the grouping was saved.",
  },
  thumbnailsUnavailable: {
    id: 'editScreen.pageGrouping.thumbnailsUnavailable',
    defaultMessage: 'Page thumbnails unavailable',
    description: 'Heading of the alert in the Edit Page Grouping dialog shown when the page thumbnails could not be loaded.',
  },
  save: {
    id: 'editScreen.pageGrouping.save',
    defaultMessage: 'Save page grouping',
    description: 'Button of the Edit Page Grouping dialog that saves the grouping.',
  },
  saveAndReextract: {
    id: 'editScreen.pageGrouping.saveAndReextract',
    defaultMessage: 'Save and Re-extract',
    description:
      'Button of the Edit Page Grouping dialog on the document page: saves the grouping, then extracts again the sections whose class or pages changed, after a confirmation.',
  },
  close: {
    id: 'editScreen.pageGrouping.close',
    defaultMessage: 'Close',
    description: 'Button that closes the dialog reporting a re-extract that failed after Save and Re-extract saved the grouping.',
  },
  reextractAgain: {
    id: 'editScreen.pageGrouping.reextractAgain',
    defaultMessage: 'Re-extract again',
    description:
      'Button of the dialog reporting a re-extract that failed after Save and Re-extract saved the grouping: tries the re-extract again.',
  },
  partialFailed: {
    id: 'editScreen.pageGrouping.partialFailed',
    defaultMessage: 'The page grouping was saved, but the sections were not re-extracted: {message}',
    description:
      'Error in the dialog shown when Save and Re-extract saved the grouping but the re-extract failed, above the list of those sections. {message} is why, as the request reported it, shown as is.',
  },
  sectionLine: {
    id: 'editScreen.pageGrouping.sectionLine',
    defaultMessage: '{count, plural, one {Section {id}: {class}, page {pages}} other {Section {id}: {class}, pages {pages}}}',
    description:
      "A section in the list of the dialog reporting a re-extract that failed after Save and Re-extract saved the grouping. {id} is the section's number; {class} is its document class, shown as is (empty for a section with no class); {count} is how many pages it has; {pages} lists their numbers, separated by commas.",
  },
});

/** A section's line in the list of sections a failed re-extract left. */
const sectionLine = (s: GroupedSection, intl: IntlShape): string =>
  intl.formatMessage(messages.sectionLine, {
    count: s.pageIds.length,
    id: s.sectionId,
    class: s.documentClass ?? '',
    pages: s.pageIds.join(', '),
  });

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
  const intl = useIntl();
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
      title={intl.formatMessage(messages.title)}
      pages={boardPages}
      sections={board.sections(openSectionId, openData)}
      classOptions={board.classOptions}
      canChangeClass={board.canChangeClass}
      consequence={
        <>
          {error && <Alert type="error">{error}</Alert>}
          {thumbnailFailure && (
            <Alert
              type={thumbnailFailure === 'access-denied' ? 'warning' : 'error'}
              header={intl.formatMessage(messages.thumbnailsUnavailable)}
            >
              <FormattedMessage {...THUMBNAIL_FAILURE_MESSAGES[thumbnailFailure]} />
            </Alert>
          )}
          {board.consequence}
        </>
      }
      saveLabel={intl.formatMessage(messages.save)}
      secondarySave={
        reextractBoard && {
          label: intl.formatMessage(messages.saveAndReextract),
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
          header={intl.formatMessage(messages.title)}
          onDismiss={() => setPartial(null)}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setPartial(null)}>
                  <FormattedMessage {...messages.close} />
                </Button>
                <Button variant="primary" loading={isRetrying} onClick={() => void retry()}>
                  <FormattedMessage {...messages.reextractAgain} />
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="s">
            <Alert type="error">
              <FormattedMessage {...messages.partialFailed} values={{ message: partial.message }} />
            </Alert>
            <ul>
              {partial.changed.map((s) => (
                <li key={s.sectionId}>{sectionLine(s, intl)}</li>
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
