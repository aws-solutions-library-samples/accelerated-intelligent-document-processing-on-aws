// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// More Actions › Edit Page Text (and a page row's ⋮ Edit Page Text): the page text
// editor, whose Save and Reprocess uploads the page's text, then reprocesses the
// document (processChanges with `textModified`), so the sections holding the page are
// extracted again. If the reprocess fails, the editor stays open with Retry reprocess and
// Restore original text, so the text is never left changed without saying so.
import React from 'react';

import useAppContext from '../../contexts/app';
import { submitProcessChanges } from '../common/process-changes';
import PageTextEditorModal from '../pages-panel/PageTextEditorModal';
import { documentName } from '../review-queues/review-api';

interface EditPageTextDialogProps {
  objectKey: string;
  pages: { Id: string | number; ImageUri?: string; TextUri?: string; TextConfidenceUri?: string; OcrPageDataUri?: string }[];
  initialPageId: string;
  /** Why saving can't run now (the live gate). */
  disabledReason?: string;
  onDone: () => void;
  /** Closed with the text saved but the document not reprocessed. */
  onUnfinished: (page: { pageId: string; textUri: string }) => void;
  onDismiss: () => void;
}

/** Reprocess after page `pageId`'s text changed. `newTextUri` is the text's own URI: it is rewritten in place. */
export const reprocessPageText = (objectKey: string, page: { pageId: string; textUri: string }): Promise<void> =>
  submitProcessChanges(objectKey, [], [{ pageId: Number(page.pageId), textModified: true, classReset: false, newTextUri: page.textUri }]);

const EditPageTextDialog = ({
  objectKey,
  pages,
  initialPageId,
  disabledReason,
  onDone,
  onUnfinished,
  onDismiss,
}: EditPageTextDialogProps): React.JSX.Element => {
  const { setSuccessMessage } = useAppContext();
  const name = documentName(objectKey);
  return (
    <PageTextEditorModal
      visible
      pages={pages}
      initialPageId={initialPageId}
      isReadOnly={false}
      saveLabel="Save and Reprocess"
      saveDisabledReason={disabledReason}
      saveNotice="Save and Reprocess saves this page's text, then extracts again the sections that hold the page, replacing their values, including any corrected by hand. If the document is waiting for review, its review is marked as completed, and its rule validation results are cleared."
      onSave={async (page) => {
        await reprocessPageText(objectKey, page);
        setSuccessMessage(`Saved page ${page.pageId}'s text of ${name}. The sections that hold it are being extracted again.`);
        onDone();
      }}
      onUnfinished={onUnfinished}
      onClose={onDismiss}
    />
  );
};

export default EditPageTextDialog;
