// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's models for the states before a mode is chosen, or when none can be:
// loading, a missing or unreadable document, an unreadable review item, and an
// Annotator-only user who is not a member of the item's queue. They show Home only in
// the breadcrumbs and highlight no navigation entry, since no mode has been chosen.
// The document page shows the same document states (through LoadState).
import React from 'react';
import { Link } from '@cloudscape-design/components';

import { describeApiError } from '../../hooks/utils/graphql-error';
import { DOCUMENTS_PATH, MY_QUEUES_PATH, MY_TASKS_PATH } from '../../routes/constants';
import { getErrorMessage } from '../../utils/errorUtils';
import { HOME_CRUMB } from '../review-queues/breadcrumbs';
import type { EditFailure, EditLoading, EditModel, EditNavigation } from './edit-model';

const NO_MODE: EditNavigation = { trail: [HOME_CRUMB], activeHref: null };

/** Where a user goes back to: Documents, or Tasks for an Annotator-only user, who has no Documents. */
const backLink = (annotatorOnly: boolean): React.ReactNode =>
  annotatorOnly ? <Link href={`#${MY_TASKS_PATH}`}>Back to Tasks</Link> : <Link href={`#${DOCUMENTS_PATH}`}>Back to Documents</Link>;

export const loadingDocumentModel = (): EditModel & { state: EditLoading } => ({
  navigation: NO_MODE,
  state: { kind: 'loading', text: 'Loading document…' },
});

export const documentMissingModel = (annotatorOnly: boolean): EditModel & { state: EditFailure } => ({
  navigation: NO_MODE,
  state: {
    kind: 'failure',
    type: 'warning',
    header: 'Document not found',
    body: <>This document no longer exists. {backLink(annotatorOnly)}</>,
  },
});

export const documentFailedModel = (error: unknown, retry: () => void, annotatorOnly: boolean): EditModel & { state: EditFailure } => ({
  navigation: NO_MODE,
  state: {
    kind: 'failure',
    type: 'error',
    header: "Couldn't open this document",
    body: (
      <>
        {getErrorMessage(error) || 'The document could not be loaded.'} {backLink(annotatorOnly)}
      </>
    ),
    retry,
  },
});

export const itemFailedModel = (error: unknown, retry: () => void): EditModel => ({
  navigation: NO_MODE,
  state: {
    kind: 'failure',
    type: 'error',
    header: "Couldn't open this review item",
    body: describeApiError(error, 'load the review item'),
    retry,
  },
});

export const notAMemberModel = (): EditModel => ({
  navigation: NO_MODE,
  state: {
    kind: 'failure',
    type: 'warning',
    header: "You can't open this review item",
    body: (
      <>
        Only the Annotators listed on this item&apos;s queue can open it. The queues that list you are in{' '}
        <Link href={`#${MY_QUEUES_PATH}`}>Home › Queues</Link>.
      </>
    ),
  },
});
