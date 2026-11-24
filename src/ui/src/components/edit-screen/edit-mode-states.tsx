// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's models for the states before a mode is chosen, or when none can be:
// loading, a missing or unreadable document, an unreadable review item, and an
// Annotator-only user who is not a member of the item's queue. They show Home only in
// the breadcrumbs and highlight no navigation entry, since no mode has been chosen.
// The document page shows the same document states (through LoadState). Each is worded
// with the `intl` its caller passes, at render.
import React from 'react';
import { Link } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type MessageValue, type NoMessageValues } from 'react-intl';

import { describeApiError } from '../../hooks/utils/graphql-error';
import { DOCUMENTS_PATH, MY_QUEUES_PATH, MY_TASKS_PATH } from '../../routes/constants';
import { getErrorMessage } from '../../utils/errorUtils';
import { homeCrumb } from '../review-queues/breadcrumbs';
import { editScreenSharedMessages } from './edit-messages';
import type { EditFailure, EditLoading, EditModel, EditNavigation } from './edit-model';

const statesMessages = defineMessages<{
  documentMissingBody: { readonly backLink: MessageValue };
  documentFailedBody: { readonly error: string; readonly backLink: MessageValue };
  itemFailedHeader: NoMessageValues;
  loadReviewItem: NoMessageValues;
  notAMemberHeader: NoMessageValues;
  notAMemberBody: { readonly queuesLink: MessageValue };
  queuesLink: NoMessageValues;
}>({
  documentMissingBody: {
    id: 'editScreen.states.documentMissing.body',
    defaultMessage: 'This document no longer exists. {backLink}',
    description:
      'Text of the warning the edit page or the document page shows when its document no longer exists. {backLink} is a link back to the Documents list or to Home › Tasks.',
  },
  documentFailedBody: {
    id: 'editScreen.states.documentFailed.body',
    defaultMessage: '{error} {backLink}',
    description:
      'Text of the error the edit page or the document page shows when its document could not be read. {error} is the reason the server gave, as it sent it, or a sentence saying the document could not be loaded; {backLink} is a link back to the Documents list or to Home › Tasks.',
  },
  itemFailedHeader: {
    id: 'editScreen.states.itemFailed.header',
    defaultMessage: "Couldn't open this review item",
    description: 'Heading of the error the edit page shows when the review queue item of its document could not be read.',
  },
  loadReviewItem: {
    id: 'editScreen.states.itemFailed.action',
    defaultMessage: 'load the review item',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when the edit page could not read the review queue item of its document.',
  },
  notAMemberHeader: {
    id: 'editScreen.states.notAMember.header',
    defaultMessage: "You can't open this review item",
    description: 'Heading of the warning the edit page shows to an Annotator who is not listed on the review queue of the item.',
  },
  notAMemberBody: {
    id: 'editScreen.states.notAMember.body',
    defaultMessage: "Only the Annotators listed on this item's queue can open it. The queues that list you are in {queuesLink}.",
    description:
      'Text of the warning the edit page shows to an Annotator who is not listed on the review queue of the item. {queuesLink} is a link to Home › Queues, the page of the queues that list the user.',
  },
  queuesLink: {
    id: 'editScreen.states.notAMember.queuesLink',
    defaultMessage: 'Home › Queues',
    description:
      'Text of the link to the page of the queues that list the user, named by its breadcrumb trail, inside the warning to an Annotator who cannot open a review item.',
  },
});

const noMode = (intl: IntlShape): EditNavigation => ({ trail: [homeCrumb(intl)], activeHref: null });

/** Where a user goes back to: Documents, or Tasks for an Annotator-only user, who has no Documents. */
const backLink = (annotatorOnly: boolean, intl: IntlShape): React.ReactNode =>
  annotatorOnly ? (
    <Link href={`#${MY_TASKS_PATH}`}>{intl.formatMessage(editScreenSharedMessages.backToTasks)}</Link>
  ) : (
    <Link href={`#${DOCUMENTS_PATH}`}>{intl.formatMessage(editScreenSharedMessages.backToDocuments)}</Link>
  );

export const loadingDocumentModel = (intl: IntlShape): EditModel & { state: EditLoading } => ({
  navigation: noMode(intl),
  state: { kind: 'loading', text: intl.formatMessage(editScreenSharedMessages.loadingDocument) },
});

export const documentMissingModel = (annotatorOnly: boolean, intl: IntlShape): EditModel & { state: EditFailure } => ({
  navigation: noMode(intl),
  state: {
    kind: 'failure',
    type: 'warning',
    header: intl.formatMessage(editScreenSharedMessages.documentNotFound),
    body: intl.formatMessage(statesMessages.documentMissingBody, { backLink: backLink(annotatorOnly, intl) }),
  },
});

export const documentFailedModel = (
  error: unknown,
  retry: () => void,
  annotatorOnly: boolean,
  intl: IntlShape,
): EditModel & { state: EditFailure } => ({
  navigation: noMode(intl),
  state: {
    kind: 'failure',
    type: 'error',
    header: intl.formatMessage(editScreenSharedMessages.couldNotOpenDocument),
    body: intl.formatMessage(statesMessages.documentFailedBody, {
      error: getErrorMessage(error) || intl.formatMessage(editScreenSharedMessages.documentCouldNotLoad),
      backLink: backLink(annotatorOnly, intl),
    }),
    retry,
  },
});

export const itemFailedModel = (error: unknown, retry: () => void, intl: IntlShape): EditModel => ({
  navigation: noMode(intl),
  state: {
    kind: 'failure',
    type: 'error',
    header: intl.formatMessage(statesMessages.itemFailedHeader),
    body: describeApiError(error, statesMessages.loadReviewItem, intl),
    retry,
  },
});

export const notAMemberModel = (intl: IntlShape): EditModel => ({
  navigation: noMode(intl),
  state: {
    kind: 'failure',
    type: 'warning',
    header: intl.formatMessage(statesMessages.notAMemberHeader),
    body: intl.formatMessage(statesMessages.notAMemberBody, {
      queuesLink: <Link href={`#${MY_QUEUES_PATH}`}>{intl.formatMessage(statesMessages.queuesLink)}</Link>,
    }),
  },
});
