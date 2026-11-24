// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's model for a Test Studio document: the test-set adapter and the Test
// Studio slots over the shared editor, with Save, Skip, Cancel and Process Changes and
// the Edit | View switch. Save keeps the open section's edits as a draft, which is not
// reviewed: any later session, and the next assignee after a Skip, opens with it.
// Process Changes saves the open section's edits, then applies every section (its draft,
// or its labels as they are) and leaves once the server counts the document reviewed,
// so "the labels are correct" and "I corrected them" end the same way. For a document
// with a review copy, completing its last section re-runs evaluation and summarization
// on that copy (not OCR, classification or extraction); authored ground truth has no
// copy, so Process Changes only marks it reviewed. Skip gives the document
// back to its queue, unassigned and not reviewed, for another Annotator to claim. The
// assignee works the document; an Admin or Author may too. An Annotator listed on the
// set's queue can open any document read-only, and Claim an unassigned one to work it.
import React, { useCallback, useEffect, useState } from 'react';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type MessageTag, type NoMessageValues } from 'react-intl';
import { useLocation, useSearchParams } from 'react-router-dom';
import { Box, Button, Link } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { MY_QUEUES_PATH, parseEditOrigin, testSetAnnotateHref, testSetDetailHref, testSetQueuePath } from '../../routes/constants';
import { getFieldsBelowThreshold, getHitlConfidenceThreshold } from '../common/confidence-alerts-utils';
import { sharedLabelMessages } from '../common/labels';
import { editActiveHref, editTrail } from '../review-queues/breadcrumbs';
import { documentName } from '../review-queues/review-api';
import { claimTestSetDocument } from '../review-queues/reviewer-assignment';
import { testSetWorkItemId } from '../review-queues/work-item-ids';
import {
  DRAFT_PROMPT_BODY,
  DRAFT_PROMPT_HEADER,
  annotationEditFlags,
  draftBadgeHover,
  groundTruthAlertSuffix,
  mayChangeClassFor,
  versionBadgeText,
} from '../test-studio/annotation-edit-flags';
import type { EditAction } from './EditScreen';
import { EditPageError, editPageErrorText, editScreenSharedMessages } from './edit-messages';
import type { EditModel, EditNavigation, ReviewActivity } from './edit-model';
import { useTestSetItemEdit } from './test-set-item-edit-adapter';
import { useTestSetEditorSlots } from './test-set-editor-slots';
import { useLeavePath } from './use-edit-exit';
import type { EditorSession } from './use-editor-session';

type Name = { readonly name: string };
type ProcessChanges = { readonly processChanges: string };

const messages = defineMessages<{
  regroupPending: NoMessageValues;
  regroupRereadFailed: NoMessageValues;
  reviewActivityAssigned: { readonly assignee: string };
  savedDraft: Name & ProcessChanges;
  notReviewed: Name & ProcessChanges;
  claimFirstToRegroup: NoMessageValues;
  roleCannotRegroup: NoMessageValues;
  testSetCrumb: Name;
  notAvailableHeader: NoMessageValues;
  notAvailableBody: NoMessageValues;
  cannotOpenHeader: NoMessageValues;
  cannotOpenBody: { readonly link: MessageTag };
  loadDocumentAction: NoMessageValues;
  loading: NoMessageValues;
  documentGone: { readonly link: MessageTag };
  staleDraft: NoMessageValues;
  viewOnlyAssigned: { readonly assignee: string };
  viewOnlyClaim: NoMessageValues;
  viewOnlyUnassigned: NoMessageValues;
  viewOnlyRole: NoMessageValues;
  alreadyReviewed: NoMessageValues;
  alreadySkipped: NoMessageValues;
  notAssignedToYou: NoMessageValues;
  loadingShort: NoMessageValues;
  saveFirst: NoMessageValues;
  noLabelsSkipCompletes: NoMessageValues;
  roleCannotSave: NoMessageValues;
  noSectionsSkipCompletes: NoMessageValues;
  statusReviewed: NoMessageValues;
  statusSkipped: NoMessageValues;
  statusToReview: NoMessageValues;
  bucketMissing: NoMessageValues;
  alertCount: { readonly count: number };
  notReady: NoMessageValues;
  authoredGroundTruth: ProcessChanges;
  primaryHintReprocess: NoMessageValues;
  primaryHintAuthored: NoMessageValues;
  unsavedChanges: NoMessageValues;
  noSections: { readonly objectKey: string };
  skipHeader: NoMessageValues;
  skipBody: NoMessageValues;
  processingReviewed: Name;
  completeReviewed: Name;
}>({
  regroupPending: {
    id: 'testStudio.itemEdit.regroupPending',
    defaultMessage: 'Loading the saved grouping…',
    description:
      'Why Edit Page Grouping is unavailable: a page grouping was just saved and the document editor is still re-reading the saved sections.',
  },
  regroupRereadFailed: {
    id: 'testStudio.itemEdit.regroupRereadFailed',
    defaultMessage: 'The saved grouping could not be re-read. Choose Refresh to load it.',
    description:
      'Why Edit Page Grouping is unavailable: re-reading the sections after a page grouping was saved failed. Refresh is a button on the same page.',
  },
  reviewActivityAssigned: {
    id: 'testStudio.itemEdit.reviewActivity.assigned',
    defaultMessage: 'Assigned to {assignee}',
    description:
      "Note of the assignment row in the Review Activity tab of a test-set item's History, on its edit page. {assignee} is the e-mail address of the user the document is assigned to.",
  },
  savedDraft: {
    id: 'testStudio.itemEdit.savedDraft',
    defaultMessage: 'Saved your changes to {name} as a draft. {processChanges} writes them as reviewed ground truth.',
    description:
      "Notice after Save on a test-set item's edit page kept the open section's changes as a draft. {name} is the file name of the item's document; {processChanges} is the name of the page's action, Process Changes.",
  },
  notReviewed: {
    id: 'testStudio.itemEdit.notReviewed',
    defaultMessage: 'Not every section of {name} is marked reviewed yet. Check its sections and choose {processChanges} again.',
    description:
      "Error after Process Changes on a test-set item's edit page applied every section, but the server does not count the document reviewed yet, so the page stays open. {name} is the file name of the item's document; {processChanges} is the name of the page's action, Process Changes.",
  },
  claimFirstToRegroup: {
    id: 'testStudio.itemEdit.claimFirstToRegroup',
    defaultMessage: 'Claim this document first to change its page grouping',
    description:
      "Why Edit Page Grouping is unavailable on an unassigned test-set item's edit page: only the document's holder may change its page grouping.",
  },
  roleCannotRegroup: {
    id: 'testStudio.itemEdit.roleCannotRegroup',
    defaultMessage: 'Your role cannot change this document’s page grouping',
    description: "Why Edit Page Grouping is unavailable on a test-set item's edit page: the user's role may not change the page grouping.",
  },
  testSetCrumb: {
    id: 'testStudio.itemEdit.testSetCrumb',
    defaultMessage: 'Test set: {name}',
    description:
      "Breadcrumb of the test set's queue page in the trail of a test-set item's edit page, after Review Management or Home › Queues. {name} is the test set's name.",
  },
  notAvailableHeader: {
    id: 'testStudio.itemEdit.notAvailableHeader',
    defaultMessage: 'Not available for your account',
    description: "Heading of the error a test-set item's edit page shows to a user whose roles cannot edit ground truth.",
  },
  notAvailableBody: {
    id: 'testStudio.itemEdit.notAvailableBody',
    defaultMessage: 'Editing ground truth requires an Annotator, Author or Admin role.',
    description: "Text of the error a test-set item's edit page shows to a user whose roles cannot edit ground truth.",
  },
  cannotOpenHeader: {
    id: 'testStudio.itemEdit.cannotOpenHeader',
    defaultMessage: "You can't open this document",
    description: "Heading of the warning a test-set item's edit page shows when the server refused to read the document for this user.",
  },
  cannotOpenBody: {
    id: 'testStudio.itemEdit.cannotOpenBody',
    defaultMessage:
      "Only the Annotators listed on this test set's queue can open its documents. The queues that list you are in <link>Home › Queues</link>.",
    description:
      "Text of the warning a test-set item's edit page shows when the server refused to read the document for this user. <link>…</link> marks the link to Home › Queues, the page of the queues that list the user, named by its breadcrumb trail.",
  },
  loadDocumentAction: {
    id: 'testStudio.itemEdit.action.loadDocument',
    defaultMessage: 'load the document',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}") when a test-set item\'s edit page could not read its document.',
  },
  loading: {
    id: 'testStudio.itemEdit.loading',
    defaultMessage: 'Loading the document…',
    description: "Shown beside a spinner while a test-set item's edit page reads its document.",
  },
  documentGone: {
    id: 'testStudio.itemEdit.documentGone',
    defaultMessage: "This document isn't in the test set any more. <link>Back to the test set</link>",
    description:
      "Text of the error a test-set item's edit page shows when its document is no longer in the test set. <link>…</link> marks the link back to the test set's queue page.",
  },
  staleDraft: {
    id: 'testStudio.itemEdit.staleDraft',
    defaultMessage: 'Someone saved newer changes to this section. It has been reloaded; reapply your last change.',
    description: "Error on a test-set item's edit page when a save found a newer draft of the open section, which the page then reloaded.",
  },
  viewOnlyAssigned: {
    id: 'testStudio.itemEdit.viewOnlyAssigned',
    defaultMessage: 'View only. This document is assigned to {assignee}.',
    description:
      "Read-only notice at the top of a test-set item's edit page when the document is assigned to another user. {assignee} is that user's e-mail address.",
  },
  viewOnlyClaim: {
    id: 'testStudio.itemEdit.viewOnlyClaim',
    defaultMessage: 'View only. Claim this document to work on it; it is then assigned to you and shows in Home › Tasks.',
    description:
      "Read-only notice at the top of an unassigned test-set item's edit page, beside its Claim button. Home › Tasks is the page that lists the user's work, named by its breadcrumb trail.",
  },
  viewOnlyUnassigned: {
    id: 'testStudio.itemEdit.viewOnlyUnassigned',
    defaultMessage: 'View only. It is not assigned to you.',
    description: "Read-only notice at the top of an unassigned test-set item's edit page when the user may not claim the document.",
  },
  viewOnlyRole: {
    id: 'testStudio.itemEdit.viewOnlyRole',
    defaultMessage: 'View only. Your role cannot edit these labels.',
    description: "Read-only notice at the top of a test-set item's edit page when the user's role may not edit the document's labels.",
  },
  alreadyReviewed: {
    id: 'testStudio.itemEdit.alreadyReviewed',
    defaultMessage: 'This document is already reviewed',
    description: "Why Skip and Process Changes are unavailable on a test-set item's edit page: the document is already reviewed.",
  },
  alreadySkipped: {
    id: 'testStudio.itemEdit.alreadySkipped',
    defaultMessage: 'This document was already skipped',
    description: "Why Skip and Process Changes are unavailable on a test-set item's edit page: the document was already skipped.",
  },
  notAssignedToYou: {
    id: 'testStudio.itemEdit.notAssignedToYou',
    defaultMessage: 'This document is not assigned to you',
    description: "Why Skip and Process Changes are unavailable on an unassigned test-set item's edit page.",
  },
  loadingShort: {
    id: 'testStudio.itemEdit.loadingShort',
    defaultMessage: 'Loading…',
    description: "Why Skip, Refresh or Process Changes is unavailable on a test-set item's edit page while the document is being read.",
  },
  saveFirst: {
    id: 'testStudio.itemEdit.saveFirst',
    defaultMessage: 'Save your changes first',
    description: "Why Skip is unavailable on a test-set item's edit page: the open section has unsaved changes.",
  },
  noLabelsSkipCompletes: {
    id: 'testStudio.itemEdit.noLabelsSkipCompletes',
    defaultMessage: 'This document has no labels yet; Skip completes it',
    description:
      "Why Process Changes is unavailable on a test-set item's edit page: the document has no labels yet. Skip is the button on the same bar.",
  },
  roleCannotSave: {
    id: 'testStudio.itemEdit.roleCannotSave',
    defaultMessage: 'Your role cannot save these labels',
    description: "Why Process Changes is unavailable on a test-set item's edit page: the user's role may not save the document's labels.",
  },
  noSectionsSkipCompletes: {
    id: 'testStudio.itemEdit.noSectionsSkipCompletes',
    defaultMessage: 'This document has no sections; Skip completes it',
    description:
      "Why Process Changes is unavailable on a test-set item's edit page: the document has no sections. Skip is the button on the same bar.",
  },
  statusReviewed: {
    id: 'testStudio.itemEdit.status.reviewed',
    defaultMessage: 'Reviewed',
    description: "Status in parentheses after the document's name in the heading of a test-set item's edit page: the document is reviewed.",
  },
  statusSkipped: {
    id: 'testStudio.itemEdit.status.skipped',
    defaultMessage: 'Skipped',
    description: "Status in parentheses after the document's name in the heading of a test-set item's edit page: the document was skipped.",
  },
  statusToReview: {
    id: 'testStudio.itemEdit.status.toReview',
    defaultMessage: 'To review',
    description:
      "Status in parentheses after the document's name in the heading of a test-set item's edit page: the document is neither reviewed nor skipped.",
  },
  bucketMissing: {
    id: 'testStudio.itemEdit.bucketMissing',
    defaultMessage: 'TestSetBucket is not configured in settings.',
    description:
      "Error at the top of a test-set item's edit page when the app's settings name no test-set bucket. TestSetBucket is the setting's name.",
  },
  alertCount: {
    id: 'testStudio.itemEdit.alertCount',
    defaultMessage:
      '{count, plural, one {# field is below the confidence threshold.} other {# fields are below the confidence threshold.}}',
    description:
      "Notice at the top of a test-set item's edit page, beside its Next alert button. {count} is the number of the document's fields below the confidence threshold, 1 or more.",
  },
  notReady: {
    id: 'testStudio.itemEdit.notReady',
    defaultMessage: 'Not ready to edit: this document has no labels yet. Generate draft labels for the set first.',
    description: "Warning at the top of a test-set item's edit page when the document has neither a review copy nor labels.",
  },
  authoredGroundTruth: {
    id: 'testStudio.itemEdit.authoredGroundTruth',
    defaultMessage: 'Authored ground truth: check its labels, correct them if needed, then choose {processChanges}.',
    description:
      "First sentence of the notice at the top of a test-set item's edit page when the document's labels are authored ground truth, with no review copy. {processChanges} is the name of the page's action, Process Changes.",
  },
  primaryHintReprocess: {
    id: 'testStudio.itemEdit.primaryHintReprocess',
    defaultMessage:
      'Apply your saved changes and mark the labels reviewed (the rest as correct): evaluation and summarization re-run; OCR, classification and extraction are not repeated.',
    description:
      "Hint of Process Changes in the action bar of a test-set item's edit page, for a document with a review copy: what it does.",
  },
  primaryHintAuthored: {
    id: 'testStudio.itemEdit.primaryHintAuthored',
    defaultMessage:
      'Apply your saved changes and mark the labels reviewed (the rest as correct). Authored ground truth has no review copy, so nothing is reprocessed.',
    description:
      "Hint of Process Changes in the action bar of a test-set item's edit page, for authored ground truth, which has no review copy: what it does.",
  },
  unsavedChanges: {
    id: 'testStudio.itemEdit.unsavedChanges',
    defaultMessage: 'You have unsaved ground truth changes. Leave this document and discard them?',
    description: "Question a test-set item's edit page asks before the user leaves it with unsaved changes in the open section.",
  },
  noSections: {
    id: 'testStudio.itemEdit.noSections',
    defaultMessage: 'No ground truth (baseline) sections found for {objectKey}.',
    description:
      "Shown in the section editor's place on a test-set item's edit page when the document has no sections. {objectKey} is the document's key in the test set.",
  },
  skipHeader: {
    id: 'testStudio.itemEdit.skipHeader',
    defaultMessage: 'Skip this document',
    description: "Title of the dialog that Skip opens on a test-set item's edit page, which asks why the document is skipped.",
  },
  skipBody: {
    id: 'testStudio.itemEdit.skipBody',
    defaultMessage: 'It goes back to its queue, unassigned and not reviewed, for another Annotator to claim.',
    description: "Text of the dialog that Skip opens on a test-set item's edit page: what skipping the document does.",
  },
  processingReviewed: {
    id: 'testStudio.itemEdit.processingReviewed',
    defaultMessage: 'Processing the changes to {name}. Its labels are marked reviewed.',
    description:
      "Notice shown as a test-set item's edit page closes after Process Changes, for a document with a review copy. {name} is the file name of the item's document.",
  },
  completeReviewed: {
    id: 'testStudio.itemEdit.completeReviewed',
    defaultMessage: '{name} is complete. Its labels are marked reviewed.',
    description:
      "Notice shown as a test-set item's edit page closes after Process Changes, for authored ground truth. {name} is the file name of the item's document.",
  },
});

/**
 * What a test-set item's failed action was trying to do: a phrase placed in the shared
 * error sentence.
 */
const failedActionMessages = defineMessages<Record<EditAction, NoMessageValues>>({
  claim: {
    id: 'testStudio.itemEdit.action.claim',
    defaultMessage: 'claim this document',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Claim failed on a test-set item\'s edit page.',
  },
  skip: {
    id: 'testStudio.itemEdit.action.skip',
    defaultMessage: 'skip this document',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Skip failed on a test-set item\'s edit page.',
  },
  process: {
    id: 'testStudio.itemEdit.action.process',
    defaultMessage: 'process the changes to this document',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Process Changes failed on a test-set item\'s edit page.',
  },
  save: {
    id: 'testStudio.itemEdit.action.save',
    defaultMessage: 'save this section',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Save failed on a test-set item\'s edit page.',
  },
  regroup: {
    id: 'testStudio.itemEdit.action.regroup',
    defaultMessage: 'edit the page grouping',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Edit Page Grouping failed on a test-set item\'s edit page.',
  },
  baseline: {
    id: 'testStudio.itemEdit.action.baseline',
    defaultMessage: 'use this document as the evaluation baseline',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Use As Evaluation Baseline failed on a test-set item\'s edit page.',
  },
  cancel: {
    id: 'testStudio.itemEdit.action.cancel',
    defaultMessage: 'leave this document',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Cancel failed on a test-set item\'s edit page.',
  },
  refresh: {
    id: 'testStudio.itemEdit.action.refresh',
    defaultMessage: 'reload this document',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Refresh failed on a test-set item\'s edit page.',
  },
});

/**
 * A test-set document's review activity, from what its queue row records (its
 * assignment and verdict); the field edits are in the label's own `_editHistory`.
 */
const testSetReviewHistory = (
  doc: {
    assignedTo?: string | null;
    assignedAt?: string | null;
    reviewed?: boolean | null;
    skipped?: boolean | null;
  },
  intl: IntlShape,
): ReviewActivity =>
  doc.assignedAt && doc.assignedTo
    ? [
        {
          at: doc.assignedAt,
          event: 'assigned',
          actor: null,
          details: JSON.stringify({ note: intl.formatMessage(messages.reviewActivityAssigned, { assignee: doc.assignedTo }) }),
        },
      ]
    : [];

/** The server's verdict after Process Changes: not reviewed yet, so the page stays. */
class NotReviewedYet extends Error {}

export const useTestSetItemEditModel = (testSetId: string, objectKey: string, session: EditorSession): EditModel => {
  const intl = useIntl();
  const adapter = useTestSetItemEdit(testSetId, objectKey);
  const { doc, draft, queue, loading, loadError, reload, notice, clearNotice } = adapter;
  const { setErrorMessage } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, isAuthor, isReviewer, isAnnotator, canAnnotate, canWorkTestSetQueues, loading: roleLoading } = useUserRole();
  const leavePath = useLeavePath();
  const reviewWork = useOptionalReviewWork();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const focusField = searchParams.get('field');
  const initialSectionId = searchParams.get('section');
  // The open section's alert Next alert moves to next.
  const [nextAlert, setNextAlert] = useState(0);

  // A save's notice (a newer draft was found and reloaded), worded here with `intl` and then
  // cleared, so the saving callback never words it.
  useEffect(() => {
    if (!notice) return;
    setErrorMessage(intl.formatMessage(messages.staleDraft));
    clearNotice();
  }, [notice, clearNotice, intl, setErrorMessage]);

  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;
  const queuePath = testSetQueuePath(testSetId);
  const workItemId = testSetWorkItemId(testSetId, objectKey);
  const name = documentName(objectKey);
  const roles = { isAdmin, isAuthor, isReviewer, isAnnotator, canAnnotate };
  const editFlags = doc ? annotationEditFlags(roles, doc) : null;
  const mayWork = !!doc && (doc.claimedByMe || isAdmin || isAuthor);
  const editorReadOnly = !editFlags || editFlags.isReadOnly || !mayWork;
  const mayChangeClass = !!editFlags && mayChangeClassFor(editFlags.canChangeClass && mayWork, editorReadOnly);
  const sections = doc?.sections ?? [];
  // Who may regroup: the document's holder (an Annotator it is assigned to), an Admin or
  // an Author; an Annotator who doesn't hold it claims it first.
  const regroupReason: string | undefined = !doc
    ? undefined
    : !mayWork
      ? doc.assignedTo
        ? intl.formatMessage(editScreenSharedMessages.assignedTo, { assignee: doc.assignedTo })
        : intl.formatMessage(messages.claimFirstToRegroup)
      : !mayChangeClass || !testSetId
        ? intl.formatMessage(messages.roleCannotRegroup)
        : undefined;

  const slots = useTestSetEditorSlots({
    bucket: testSetBucket ?? '',
    inputKey: doc?.inputKey ?? '',
    objectKey: doc?.objectKey ?? objectKey,
    testSetId,
    sections,
    mayChangeClass,
    openSectionId: session.selectedSectionId,
    openData: session.sectionData,
    onChanged: () => reload(),
  });

  const buildFieldLink = useCallback(
    (fieldPath: string) => `${window.location.origin}/${testSetAnnotateHref(testSetId, objectKey)}?field=${encodeURIComponent(fieldPath)}`,
    [testSetId, objectKey],
  );

  // The trail the user came by; without one, Review Management for an Admin, Tasks for an
  // Annotator, the set in Test Studio for an Author.
  const origin = parseEditOrigin(location.search) ?? (isAdmin ? 'review' : canWorkTestSetQueues ? 'tasks' : 'test-studio');
  // The set is named once its queue has loaded, never by its id first.
  const setName = queue ? (queue.name ?? testSetId) : null;
  const navigation: EditNavigation = {
    trail: editTrail(
      {
        origin,
        objectKey,
        name,
        queue: setName
          ? { crumb: { text: intl.formatMessage(messages.testSetCrumb, { name: setName }), href: `#${queuePath}` }, view: 'test' }
          : null,
        testSet: setName ? { text: setName, href: testSetDetailHref(testSetId) } : null,
      },
      intl,
    ),
    activeHref: editActiveHref(origin),
  };

  if (!roleLoading && !canAnnotate) {
    return {
      navigation,
      state: {
        kind: 'failure',
        type: 'error',
        header: intl.formatMessage(messages.notAvailableHeader),
        body: intl.formatMessage(messages.notAvailableBody),
      },
    };
  }
  if (loadError && !doc) {
    const notAssigned = loadError === 'not-assigned';
    let body: React.ReactNode;
    if (notAssigned)
      body = (
        <FormattedMessage
          {...messages.cannotOpenBody}
          values={{ link: (chunks: React.ReactNode[]) => <Link href={`#${MY_QUEUES_PATH}`}>{chunks}</Link> }}
        />
      );
    else if (loadError === 'load-failed') body = intl.formatMessage(editScreenSharedMessages.documentCouldNotLoad);
    else body = describeApiError(loadError.cause, messages.loadDocumentAction, intl);
    return {
      navigation,
      state: {
        kind: 'failure',
        type: notAssigned ? 'warning' : 'error',
        header: intl.formatMessage(notAssigned ? messages.cannotOpenHeader : editScreenSharedMessages.couldNotOpenDocument),
        body,
        retry: notAssigned ? undefined : () => void reload(),
      },
    };
  }
  if (!queue) return { navigation, state: { kind: 'loading', text: intl.formatMessage(messages.loading) } };
  if (!doc || !editFlags) {
    return {
      navigation,
      state: {
        kind: 'failure',
        type: 'error',
        header: intl.formatMessage(editScreenSharedMessages.documentNotFound),
        body: (
          <FormattedMessage
            {...messages.documentGone}
            values={{ link: (chunks: React.ReactNode[]) => <Link href={`#${queuePath}`}>{chunks}</Link> }}
          />
        ),
      },
    };
  }

  // Confirming the last section of a review copy re-runs its evaluation and summary; authored ground truth
  // has no copy, so finishing it only marks its labels reviewed.
  const reprocesses = !!doc.reviewObjectKey;
  const { dirty, saving } = session;

  // A document with confidence alerts opens on them, with a summary and Next alert, which
  // steps through the open section's fields below the threshold.
  const alertCount = doc.alertCount ?? 0;
  const config = (slots.mergedConfig as Record<string, unknown> | null | undefined) ?? undefined;
  const explainability = session.sectionData?.explainability_info;
  const explained = (Array.isArray(explainability) ? explainability[0] : explainability) as Record<string, unknown> | undefined;
  const alertFields =
    alertCount > 0 && explained && session.selectedSectionId
      ? getFieldsBelowThreshold(
          explained,
          getHitlConfidenceThreshold(config ?? null),
          '',
          (session.sectionData?.inference_result as Record<string, unknown> | undefined) ?? undefined,
        )
      : [];
  const handleNextAlert = () => {
    if (!session.selectedSectionId || alertFields.length === 0) return;
    const at = nextAlert % alertFields.length;
    session.focusField(session.selectedSectionId, alertFields[at].fieldPath);
    setNextAlert(at + 1);
  };

  // The primary action needs no edit first: it opens the version transition itself,
  // behind a confirmation when none is open yet. Only the role half of the editor's read-only state applies: a
  // review copy is completed by an Admin or an Annotator, authored ground truth by
  // whoever may save it.
  const roleCannotSave = editFlags.saveViaReview
    ? !(editFlags.canSaveViaReview && canWorkTestSetQueues)
    : !editFlags.canSaveDirectToBaseline;
  // A completed document still opens, so its labels can be read and corrected, but its
  // task is over: completing or skipping it again would end nothing. Skip now returns a
  // document to its queue; `skipped` marks one skipped before that, which still counts
  // as complete.
  const isComplete = doc.reviewed || !!doc.skipped;
  // An Annotator (not an Admin, who assigns from the queue) claims a document nobody holds.
  const canClaim = canWorkTestSetQueues && !isAdmin && !doc.assignedTo && !isComplete;
  // Why the fields can't be edited, shown first with Claim when it applies.
  let readOnlyReason: string | undefined;
  if (!mayWork)
    readOnlyReason = doc.assignedTo
      ? intl.formatMessage(messages.viewOnlyAssigned, { assignee: doc.assignedTo })
      : intl.formatMessage(canClaim ? messages.viewOnlyClaim : messages.viewOnlyUnassigned);
  else if (editorReadOnly) readOnlyReason = intl.formatMessage(messages.viewOnlyRole);
  let skipReason: string | undefined;
  if (isComplete) skipReason = intl.formatMessage(doc.reviewed ? messages.alreadyReviewed : messages.alreadySkipped);
  else if (!mayWork)
    skipReason = doc.assignedTo
      ? intl.formatMessage(editScreenSharedMessages.assignedTo, { assignee: doc.assignedTo })
      : intl.formatMessage(messages.notAssignedToYou);
  else if (loading) skipReason = intl.formatMessage(messages.loadingShort);
  // The primary action saves the open section itself, so unsaved edits don't block it.
  let completeReason: string | undefined = skipReason;
  if (!skipReason && dirty) skipReason = intl.formatMessage(messages.saveFirst);
  if (!completeReason) {
    if (!doc.reviewObjectKey && !doc.labelSource) completeReason = intl.formatMessage(messages.noLabelsSkipCompletes);
    else if (roleCannotSave) completeReason = intl.formatMessage(messages.roleCannotSave);
    else if (sections.length === 0) completeReason = intl.formatMessage(messages.noSectionsSkipCompletes);
  }

  const savingChanges = intl.formatMessage(editScreenSharedMessages.savingChanges);
  const processChanges = intl.formatMessage(sharedLabelMessages.processChanges);
  /** Process Changes applied everything, but the server does not yet count the document reviewed. */
  const notReviewedMessage = (file: string): string => intl.formatMessage(messages.notReviewed, { name: file, processChanges });

  return {
    navigation,
    state: {
      kind: 'ready',
      header: {
        title: name,
        status: intl.formatMessage(doc.reviewed ? messages.statusReviewed : doc.skipped ? messages.statusSkipped : messages.statusToReview),
        objectKey,
        // The source document is in the test-set bucket; without it there is nothing to show.
        source: testSetBucket ? { objectKey: doc.inputKey, bucket: testSetBucket } : null,
        offerReport: true,
        // While a draft version is open: which version the corrections go into.
        badge: draft ? { text: versionBadgeText(draft), hover: draftBadgeHover(draft) } : undefined,
      },
      // One alert for the screen's notices, so the space stays given over to annotation.
      notices: [
        !testSetBucket && { id: 'bucket', type: 'error', content: intl.formatMessage(messages.bucketMissing) },
        // What each step writes, while no draft version is open and this user may edit.
        !draft &&
          !editorReadOnly && {
            id: 'draft',
            type: 'info',
            content: (
              <>
                <b>{DRAFT_PROMPT_HEADER}.</b> {DRAFT_PROMPT_BODY}
              </>
            ),
          },
        alertCount > 0 && {
          id: 'alerts',
          type: 'info',
          content: intl.formatMessage(messages.alertCount, { count: alertCount }),
          action: (
            <Button onClick={handleNextAlert} disabled={alertFields.length === 0}>
              <FormattedMessage {...sharedLabelMessages.nextAlert} />
            </Button>
          ),
        },
        !doc.reviewObjectKey &&
          !doc.labelSource && {
            id: 'no-labels',
            type: 'warning',
            content: intl.formatMessage(messages.notReady),
          },
        // The second sentence is Test Studio's groundTruthAlertSuffix, which is English.
        !doc.reviewObjectKey &&
          !!doc.labelSource && {
            id: 'authored',
            type: 'info',
            content: `${intl.formatMessage(messages.authoredGroundTruth, { processChanges })}${groundTruthAlertSuffix(editFlags.canSaveDirectToBaseline)}`,
          },
      ],
      bar: {
        primaryDisabledReason: completeReason ?? (saving ? savingChanges : undefined),
        primaryHint: intl.formatMessage(reprocesses ? messages.primaryHintReprocess : messages.primaryHintAuthored),
        showSkip: true,
        skipDisabledReason: skipReason,
        showCancel: !editorReadOnly,
        cancelDisabledReason: saving ? savingChanges : undefined,
        // Save keeps a draft; only Process Changes marks the labels reviewed.
        showSave: !editorReadOnly,
        saveDisabledReason: !dirty ? intl.formatMessage(editScreenSharedMessages.noChangesToSave) : saving ? savingChanges : undefined,
        showRefresh: true,
        refreshDisabledReason: saving ? savingChanges : loading ? intl.formatMessage(messages.loadingShort) : undefined,
        showRegroup: true,
        // While the refs lag a save the board would send stale section ids. A failed re-read
        // leaves them lagging, and the page shows that error nowhere else while a document
        // is loaded, so the reason says so rather than claiming a load is under way.
        regroupDisabledReason:
          regroupReason ??
          (slots.refsPending ? intl.formatMessage(loadError ? messages.regroupRereadFailed : messages.regroupPending) : undefined),
        showClaim: canClaim,
        offerHistory: true,
      },
      editor: testSetBucket
        ? {
            sections: slots.reviewSections,
            pages: slots.pages,
            pagesLoading: slots.pagesLoading,
            /* Read-only by this document's save path, and for anyone the document is not
               assigned to (bar Admin/Author). Editable before a version is open: Save
               keeps a draft, and Process Changes opens the version. */
            readOnly: editorReadOnly,
            readOnlyReason,
            loadSection: (sectionId) => adapter.loadSection(sectionId, slots.loadSection),
            saveSection: adapter.saveDraft,
            onLoaded: slots.onLoaded,
            initialSectionId,
            reloadToken: slots.reloadToken + adapter.reloadToken,
            focusFieldPath: focusField,
            buildFieldLink,
            mergedConfig: config,
            defaultFilter: alertCount > 0 ? 'confidence-alerts' : 'none',
            slots: {
              headerExtras: slots.headerExtras,
              classSlot: slots.classSlot,
              emptyFieldsSlot: slots.emptyFieldsSlot,
              pagesNotice: slots.pagesNotice,
            },
            unsavedChangesMessage: intl.formatMessage(messages.unsavedChanges),
            emptyText: intl.formatMessage(messages.noSections, { objectKey: doc.objectKey }),
          }
        : null,
      board: slots.canRegroup ? slots.board : null,
      history: { reviewActivity: testSetReviewHistory(doc, intl) },
      skip: {
        header: intl.formatMessage(messages.skipHeader),
        reasons: queue.skipReasons ?? [],
        body: (
          <Box>
            <FormattedMessage {...messages.skipBody} />
          </Box>
        ),
        run: async (reason, note) => {
          await adapter.skip(reason, note);
          return { to: leavePath(queuePath), message: intl.formatMessage(editScreenSharedMessages.returnedToQueue, { name }), workItemId };
        },
      },
      process: {
        // Opening a version is a commitment: confirmed first while none is open.
        confirm: draft ? undefined : { kind: 'version' },
        run: async () => {
          // The open section's unsaved edits are saved as a draft first, so they are applied.
          const editor = session.controllerRef.current;
          if (editor?.isDirty() && !(await editor.save())) return null;
          // Idempotent server-side: the version transition this completion belongs to.
          if (!draft) await adapter.openDraft();
          const { reviewed } = await adapter.applyAndComplete();
          // Leave only on the server's word, not on what this session saved.
          if (!reviewed) throw new NotReviewedYet(notReviewedMessage(name));
          return {
            to: leavePath(queuePath),
            message: intl.formatMessage(reprocesses ? messages.processingReviewed : messages.completeReviewed, { name }),
            workItemId,
          };
        },
      },
      /** Claim: the document is assigned to the caller; reload so the screen becomes editable. */
      claim: async () => {
        reviewWork?.beginMutation();
        try {
          await claimTestSetDocument(testSetId, objectKey);
        } finally {
          await Promise.all([reload(), reviewWork?.refresh()]);
        }
        return intl.formatMessage(editScreenSharedMessages.claimed, { name });
      },
      savedMessage: intl.formatMessage(messages.savedDraft, { name, processChanges }),
      refresh: adapter.refresh,
      cancelTo: leavePath(queuePath),
      failures: {
        surface: 'flash',
        // The page's own error is worded here; any other is passed on as describeApiError reads it.
        describe: (action, err) =>
          err instanceof NotReviewedYet
            ? err.message
            : describeApiError(err instanceof EditPageError ? editPageErrorText(err, intl) : err, failedActionMessages[action], intl),
        // A failed claim has re-read the document already.
        after: (action) => {
          if (action !== 'claim') void reload();
        },
      },
    },
  };
};
