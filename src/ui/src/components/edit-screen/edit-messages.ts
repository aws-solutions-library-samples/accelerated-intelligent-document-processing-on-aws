// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Texts that more than one of the edit page's files show, each declared once as a message
// and worded where it is shown.
import { defineMessages, type NoMessageValues } from 'react-intl';

export const editScreenSharedMessages = defineMessages<{
  working: NoMessageValues;
  history: NoMessageValues;
  documentEditor: NoMessageValues;
  loadingDocument: NoMessageValues;
  documentNotFound: NoMessageValues;
  couldNotOpenDocument: NoMessageValues;
  documentCouldNotLoad: NoMessageValues;
  backToTasks: NoMessageValues;
  backToDocuments: NoMessageValues;
}>({
  working: {
    id: 'editScreen.shared.working',
    defaultMessage: 'Working…',
    description: "Why the edit page's buttons, in its action bar and Edit Page Grouping, are unavailable while an action runs.",
  },
  history: {
    id: 'editScreen.shared.history',
    defaultMessage: 'History',
    description:
      "Item of the edit page's Reports menu, and the title of the dialog it opens, which lists the open section's field edits and the item's review activity.",
  },
  documentEditor: {
    id: 'editScreen.shared.documentEditor',
    defaultMessage: 'Document Editor',
    description: "Title of the edit page's Document Editor panel.",
  },
  loadingDocument: {
    id: 'editScreen.shared.loadingDocument',
    defaultMessage: 'Loading document…',
    description: 'Shown beside a spinner while the edit page or the document page reads its document.',
  },
  documentNotFound: {
    id: 'editScreen.shared.documentNotFound',
    defaultMessage: 'Document not found',
    description: 'Heading of the warning the edit page or the document page shows when its document no longer exists.',
  },
  couldNotOpenDocument: {
    id: 'editScreen.shared.couldNotOpenDocument',
    defaultMessage: "Couldn't open this document",
    description: 'Heading of the error the edit page or the document page shows when its document could not be read.',
  },
  documentCouldNotLoad: {
    id: 'editScreen.shared.documentCouldNotLoad',
    defaultMessage: 'The document could not be loaded.',
    description:
      'Fallback text of the error the edit page or the document page shows when its document could not be read: used when the server gave no reason.',
  },
  backToTasks: {
    id: 'editScreen.shared.backToTasks',
    defaultMessage: 'Back to Tasks',
    description:
      "Link after the edit page's message about a missing or unreadable document, back to Home › Tasks, for a user whose only role is Annotator.",
  },
  backToDocuments: {
    id: 'editScreen.shared.backToDocuments',
    defaultMessage: 'Back to Documents',
    description:
      "Link after the edit page's or the document page's message about a missing or unreadable document, back to the Documents list.",
  },
});
