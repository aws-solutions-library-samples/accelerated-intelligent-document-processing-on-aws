// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Texts that more than one of the edit page's files show, each declared once as a message
// and worded where it is shown; a review queue item's failed-action phrases, which its
// model and its adapter both use; and the error the edit page's own loads and saves raise.
import {
  createIntl,
  defineMessages,
  type IntlShape,
  type MessageDescriptor,
  type NoMessageValues,
  type TypedMessageDescriptor,
} from 'react-intl';

import { describeApiError } from '../../hooks/utils/graphql-error';
import { getErrorMessage } from '../../utils/errorUtils';

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
  savingChanges: NoMessageValues;
  noChangesToSave: NoMessageValues;
  assignedTo: { readonly assignee: string };
  returnedToQueue: { readonly name: string };
  claimed: { readonly name: string };
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
  savingChanges: {
    id: 'editScreen.shared.savingChanges',
    defaultMessage: 'Saving your changes…',
    description:
      "Why an action of the edit page's action bar, such as Save, Cancel, Refresh or Process Changes, is unavailable while the open section's changes are being saved.",
  },
  noChangesToSave: {
    id: 'editScreen.shared.noChangesToSave',
    defaultMessage: 'No changes to save',
    description: "Why Save, in the edit page's action bar, is unavailable: the open section has no unsaved changes.",
  },
  assignedTo: {
    id: 'editScreen.shared.assignedTo',
    defaultMessage: 'Assigned to {assignee}',
    description:
      "Why an action of the edit page, such as Process Changes or Edit Page Grouping, is unavailable: the item is assigned to another user. {assignee} is that user's e-mail address.",
  },
  returnedToQueue: {
    id: 'editScreen.shared.returnedToQueue',
    defaultMessage: '{name} returned to the queue.',
    description:
      "Notice shown as the edit page closes after Skip gave the item back to its queue. {name} is the file name of the item's document.",
  },
  claimed: {
    id: 'editScreen.shared.claimed',
    defaultMessage: 'Claimed {name}. It is in Home › Tasks.',
    description:
      "Notice after Claim on the edit page assigned the item to the user. {name} is the file name of the item's document; Home › Tasks is the page that lists the user's work, named by its breadcrumb trail.",
  },
});

/**
 * What a review queue item's failed action was trying to do: a phrase placed in the shared
 * error sentence. The item's model words its failed actions with them, and its adapter a
 * failed draft save with `save`.
 */
export const failedActionMessages = defineMessages<{
  claim: NoMessageValues;
  skip: NoMessageValues;
  baseline: NoMessageValues;
  regroup: NoMessageValues;
  process: NoMessageValues;
  save: NoMessageValues;
  cancel: NoMessageValues;
  refresh: NoMessageValues;
}>({
  claim: {
    id: 'editScreen.productionItem.action.claim',
    defaultMessage: 'claim the item',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Claim failed on a review queue item\'s edit page.',
  },
  skip: {
    id: 'editScreen.productionItem.action.skip',
    defaultMessage: 'skip the item',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Skip failed on a review queue item\'s edit page.',
  },
  baseline: {
    id: 'editScreen.productionItem.action.baseline',
    defaultMessage: 'use this document as the evaluation baseline',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Use As Evaluation Baseline failed on a review queue item\'s edit page.',
  },
  regroup: {
    id: 'editScreen.productionItem.action.regroup',
    defaultMessage: 'start editing the page grouping',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Edit Page Grouping could not open on a review queue item\'s edit page.',
  },
  process: {
    id: 'editScreen.productionItem.action.process',
    defaultMessage: 'process the changes',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Process Changes failed on a review queue item\'s edit page.',
  },
  save: {
    id: 'editScreen.productionItem.action.save',
    defaultMessage: 'save your draft',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when a draft of a review queue item\'s section could not be saved.',
  },
  cancel: {
    id: 'editScreen.productionItem.action.cancel',
    defaultMessage: 'leave the item',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Cancel failed on a review queue item\'s edit page.',
  },
  refresh: {
    id: 'editScreen.productionItem.action.refresh',
    defaultMessage: 'reload the item',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Refresh failed on a review queue item\'s edit page.',
  },
});

/** Words the English `message` of an EditPageError. */
const englishIntl = createIntl({ locale: 'en', defaultLocale: 'en' });

/**
 * What an EditPageError says: a message with its values, or a failed call, which reads as
 * describeApiError's sentence about `cause` with `action` naming what was being done.
 */
type EditPageErrorText =
  | { readonly descriptor: MessageDescriptor; readonly values?: Record<string, string> }
  | { readonly action: TypedMessageDescriptor<NoMessageValues>; readonly cause: unknown };

const word = (text: EditPageErrorText, intl: IntlShape): string =>
  'action' in text ? describeApiError(text.cause, text.action, intl) : intl.formatMessage(text.descriptor, text.values);

/**
 * An error the edit page's own code raises while it loads or saves, where `intl` does not
 * reach: it carries what it says, and editPageErrorText words it where it is shown. Its
 * `message` is the English text.
 */
export class EditPageError extends Error {
  readonly text: EditPageErrorText;

  constructor(text: EditPageErrorText) {
    super(word(text, englishIntl));
    this.text = text;
  }
}

/** The text of an error the edit page shows: an EditPageError worded with `intl`, any other error's own text. */
export const editPageErrorText = (err: unknown, intl: IntlShape): string =>
  err instanceof EditPageError ? word(err.text, intl) : getErrorMessage(err);
