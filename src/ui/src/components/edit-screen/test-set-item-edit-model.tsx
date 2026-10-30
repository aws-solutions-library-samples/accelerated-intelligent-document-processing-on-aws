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
import React, { useCallback, useState } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { Box, Button, Link } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { MY_QUEUES_PATH, parseEditOrigin, testSetAnnotateHref, testSetDetailHref, testSetQueuePath } from '../../routes/constants';
import { editActiveHref, editTrail } from '../review-queues/breadcrumbs';
import { documentName } from '../review-queues/review-api';
import { claimTestSetDocument } from '../review-queues/reviewer-assignment';
import { testSetWorkItemId } from '../review-queues/work-item-ids';
import {
  DRAFT_PROMPT_BODY,
  DRAFT_PROMPT_HEADER,
  annotationEditFlags,
  draftOpenedMessage,
  groundTruthAlertSuffix,
  mayChangeClassFor,
} from '../test-studio/annotation-edit-flags';
import type { EditAction } from './EditScreen';
import type { EditModel, EditNavigation, ReviewActivity } from './edit-model';
import { NOT_ASSIGNED_MESSAGE, useTestSetItemEdit } from './test-set-item-edit-adapter';
import { useTestSetEditorSlots } from './test-set-editor-slots';
import { useLeavePath } from './use-edit-exit';
import type { EditorSession } from './use-editor-session';

/**
 * A test-set document's review activity, from what its queue row records (its
 * assignment and verdict); the field edits are in the label's own `_editHistory`.
 */
export const testSetReviewHistory = (doc: {
  assignedTo?: string | null;
  assignedAt?: string | null;
  reviewed?: boolean | null;
  skipped?: boolean | null;
}): ReviewActivity =>
  doc.assignedAt && doc.assignedTo
    ? [{ at: doc.assignedAt, event: 'assigned', actor: null, details: JSON.stringify({ note: `Assigned to ${doc.assignedTo}` }) }]
    : [];

/** The toast after Save: a draft, which Process Changes later applies. */
export const savedDraftMessage = (name: string): string =>
  `Saved your changes to ${name}. They count as reviewed once you choose Process Changes.`;

/** Process Changes applied everything, but the server does not yet count the document reviewed. */
export const notReviewedMessage = (name: string): string =>
  `Not every section of ${name} is marked reviewed yet. Check its sections and choose Process Changes again.`;

/** The server's verdict after Process Changes: not reviewed yet, so the page stays. */
class NotReviewedYet extends Error {}

/** What a failed action was trying to do, in the error it reports. */
const FAILED_ACTION: Record<EditAction, string> = {
  claim: 'claim this document',
  skip: 'skip this document',
  process: 'process the changes to this document',
  save: 'save this section',
  regroup: 'edit the page grouping',
  baseline: 'use this document as the evaluation baseline',
  cancel: 'leave this document',
  refresh: 'reload this document',
};

export const useTestSetItemEditModel = (testSetId: string, objectKey: string, session: EditorSession): EditModel => {
  const adapter = useTestSetItemEdit(testSetId, objectKey);
  const { doc, draft, queue, loading, loadError, reload } = adapter;
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, isAuthor, isReviewer, isAnnotator, canAnnotate, loading: roleLoading } = useUserRole();
  const leavePath = useLeavePath();
  const reviewWork = useOptionalReviewWork();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const focusField = searchParams.get('field');
  const initialSectionId = searchParams.get('section');
  const [isOpeningDraft, setIsOpeningDraft] = useState(false);

  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;
  const queuePath = testSetQueuePath(testSetId);
  const workItemId = testSetWorkItemId(testSetId, objectKey);
  const name = documentName(objectKey);
  const roles = { isAdmin, isAuthor, isReviewer, isAnnotator, canAnnotate };
  const editFlags = doc ? annotationEditFlags(roles, doc, draft) : null;
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
        ? `Assigned to ${doc.assignedTo}`
        : 'Claim this document first to change its page grouping'
      : !mayChangeClass || !testSetId
        ? 'Your role cannot change this document’s page grouping'
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
    onChanged: () => void reload(),
  });

  const buildFieldLink = useCallback(
    (fieldPath: string) => `${window.location.origin}/${testSetAnnotateHref(testSetId, objectKey)}?field=${encodeURIComponent(fieldPath)}`,
    [testSetId, objectKey],
  );

  // The trail the user came by; without one, Review Management for an Admin, Tasks for an
  // Annotator, the set in Test Studio for an Author.
  const origin = parseEditOrigin(location.search) ?? (isAdmin ? 'review' : isAnnotator ? 'tasks' : 'test-studio');
  // The set is named once its queue has loaded, never by its id first.
  const setName = queue ? (queue.name ?? testSetId) : null;
  const navigation: EditNavigation = {
    trail: editTrail({
      origin,
      objectKey,
      name,
      queue: setName ? { crumb: { text: `Test set: ${setName}`, href: `#${queuePath}` }, view: 'test' } : null,
      testSet: setName ? { text: setName, href: testSetDetailHref(testSetId) } : null,
    }),
    activeHref: editActiveHref(origin),
  };

  if (!roleLoading && !canAnnotate) {
    return {
      navigation,
      state: {
        kind: 'failure',
        type: 'error',
        header: 'Not available for your account',
        body: 'Editing ground truth requires an Annotator, Author or Admin role.',
      },
    };
  }
  if (loadError && !doc) {
    const notAssigned = loadError === NOT_ASSIGNED_MESSAGE;
    return {
      navigation,
      state: {
        kind: 'failure',
        type: notAssigned ? 'warning' : 'error',
        header: notAssigned ? "You can't open this document" : "Couldn't open this document",
        body: notAssigned ? (
          <>
            Only the Annotators listed on this test set&apos;s queue can open its documents. The queues that list you are in{' '}
            <Link href={`#${MY_QUEUES_PATH}`}>Home › Queues</Link>.
          </>
        ) : (
          loadError
        ),
        retry: notAssigned ? undefined : () => void reload(),
      },
    };
  }
  if (!queue) return { navigation, state: { kind: 'loading', text: 'Loading the document…' } };
  if (!doc || !editFlags) {
    return {
      navigation,
      state: {
        kind: 'failure',
        type: 'error',
        header: 'Document not found',
        body: (
          <>
            This document isn&apos;t in the test set any more. <Link href={`#${queuePath}`}>Back to the test set</Link>
          </>
        ),
      },
    };
  }

  // Confirming the last section of a review copy re-runs its evaluation and summary; authored ground truth
  // has no copy, so finishing it only marks its labels reviewed.
  const reprocesses = !!doc.reviewObjectKey;
  const { dirty, saving } = session;

  // The primary action needs no edit and no Start editing first: it opens the version
  // transition itself. Only the role half of the editor's read-only state applies: a
  // review copy is completed by an Admin or an Annotator, authored ground truth by
  // whoever may save it.
  const roleCannotSave = editFlags.saveViaReview
    ? !(editFlags.canSaveViaReview && (isAdmin || isAnnotator))
    : !editFlags.canSaveDirectToBaseline;
  // A completed document still opens, so its labels can be read and corrected, but its
  // task is over: completing or skipping it again would end nothing. Skip now returns a
  // document to its queue; `skipped` marks one skipped before that, which still counts
  // as complete.
  const isComplete = doc.reviewed || !!doc.skipped;
  // An Annotator (not an Admin, who assigns from the queue) claims a document nobody holds.
  const canClaim = isAnnotator && !isAdmin && !doc.assignedTo && !isComplete;
  let skipReason: string | undefined;
  if (isComplete) skipReason = doc.reviewed ? 'This document is already reviewed' : 'This document was already skipped';
  else if (!mayWork) skipReason = doc.assignedTo ? `Assigned to ${doc.assignedTo}` : 'This document is not assigned to you';
  else if (loading) skipReason = 'Loading…';
  // The primary action saves the open section itself, so unsaved edits don't block it.
  let completeReason: string | undefined = skipReason;
  if (!skipReason && dirty) skipReason = 'Save your changes first';
  if (!completeReason) {
    if (!doc.reviewObjectKey && !doc.labelSource) completeReason = 'This document has no labels yet; Skip completes it';
    else if (roleCannotSave) completeReason = 'Your role cannot save these labels';
    else if (sections.length === 0) completeReason = 'This document has no sections; Skip completes it';
  }

  const handleOpenDraft = async () => {
    setIsOpeningDraft(true);
    try {
      const opened = await adapter.openDraft();
      if (opened) setSuccessMessage(draftOpenedMessage(opened));
    } catch (err) {
      setErrorMessage(describeApiError(err, 'start annotating this set'));
    } finally {
      setIsOpeningDraft(false);
    }
  };

  return {
    navigation,
    state: {
      kind: 'ready',
      header: {
        title: name,
        status: doc.reviewed ? 'Reviewed' : doc.skipped ? 'Skipped' : 'To review',
        objectKey,
        // The source document is in the test-set bucket; without it there is nothing to show.
        source: testSetBucket ? { objectKey: doc.inputKey, bucket: testSetBucket } : null,
        offerReport: true,
      },
      // One alert for the screen's notices, so the space stays given over to annotation.
      notices: [
        !testSetBucket && { id: 'bucket', type: 'error', content: 'TestSetBucket is not configured in settings.' },
        !mayWork && {
          id: 'view-only',
          type: 'info',
          content: doc.assignedTo
            ? `View only. This document is assigned to ${doc.assignedTo}.`
            : canClaim
              ? 'View only. Claim this document to work on it; it is then assigned to you and shows in Home › Tasks.'
              : 'View only. It is not assigned to you.',
        },
        !draft && {
          id: 'draft',
          type: 'info',
          content: (
            <>
              <b>{DRAFT_PROMPT_HEADER}.</b> {DRAFT_PROMPT_BODY}
            </>
          ),
          action: (
            <Button variant="primary" loading={isOpeningDraft} disabled={!canAnnotate} onClick={() => void handleOpenDraft()}>
              Start editing
            </Button>
          ),
        },
        !doc.reviewObjectKey &&
          !doc.labelSource && {
            id: 'no-labels',
            type: 'warning',
            content: 'Not ready to edit: this document has no labels yet. Generate draft labels for the set first.',
          },
        !doc.reviewObjectKey &&
          !!doc.labelSource && {
            id: 'authored',
            type: 'info',
            content: `Authored ground truth: check its labels, correct them if needed, then choose Process Changes.${groundTruthAlertSuffix(editFlags.canSaveDirectToBaseline, draft)}`,
          },
      ],
      bar: {
        primaryDisabledReason: completeReason ?? (saving ? 'Saving your changes…' : undefined),
        primaryHint: reprocesses
          ? 'Apply your saved changes and mark the labels reviewed (the rest as correct): evaluation and summarization re-run; OCR, classification and extraction are not repeated.'
          : 'Apply your saved changes and mark the labels reviewed (the rest as correct). Authored ground truth has no review copy, so nothing is reprocessed.',
        showSkip: true,
        skipDisabledReason: skipReason,
        showCancel: !editorReadOnly,
        cancelDisabledReason: saving ? 'Saving your changes…' : undefined,
        // Save keeps a draft; only Process Changes marks the labels reviewed.
        showSave: !editorReadOnly,
        saveDisabledReason: !dirty ? 'No changes to save' : saving ? 'Saving your changes…' : undefined,
        showRefresh: true,
        refreshDisabledReason: saving ? 'Saving your changes…' : loading ? 'Loading…' : undefined,
        showRegroup: true,
        regroupDisabledReason: regroupReason,
        showClaim: canClaim,
        offerHistory: true,
      },
      editor: testSetBucket
        ? {
            sections: slots.reviewSections,
            pages: slots.pages,
            pagesLoading: slots.pagesLoading,
            /* Read-only until the version transition is open, by this document's save
               path, and for anyone the document is not assigned to (bar Admin/Author). */
            readOnly: editorReadOnly,
            loadSection: (sectionId) => adapter.loadSection(sectionId, slots.loadSection),
            saveSection: adapter.saveDraft,
            onLoaded: slots.onLoaded,
            initialSectionId,
            reloadToken: slots.reloadToken + adapter.reloadToken,
            focusFieldPath: focusField,
            buildFieldLink,
            slots: {
              headerExtras: slots.headerExtras,
              classSlot: slots.classSlot,
              emptyFieldsSlot: slots.emptyFieldsSlot,
              pagesNotice: slots.pagesNotice,
            },
            unsavedChangesMessage: 'You have unsaved ground truth changes. Leave this document and discard them?',
            emptyText: `No ground truth (baseline) sections found for ${doc.objectKey}.`,
          }
        : null,
      board: slots.canRegroup ? slots.board : null,
      history: { reviewActivity: testSetReviewHistory(doc) },
      skip: {
        header: 'Skip this document',
        reasons: queue.skipReasons ?? [],
        body: <Box>It goes back to its queue, unassigned and not reviewed, for another Annotator to claim.</Box>,
        run: async (reason, note) => {
          await adapter.skip(reason, note);
          return { to: leavePath(queuePath), message: `${name} returned to the queue.`, workItemId };
        },
      },
      process: {
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
            message: reprocesses
              ? `Processing the changes to ${name}. Its labels are marked reviewed.`
              : `${name} is complete. Its labels are marked reviewed.`,
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
        return `Claimed ${name}. It is in Home › Tasks.`;
      },
      savedMessage: savedDraftMessage(name),
      refresh: adapter.refresh,
      cancelTo: leavePath(queuePath),
      failures: {
        surface: 'flash',
        describe: (action, err) => (err instanceof NotReviewedYet ? err.message : describeApiError(err, FAILED_ACTION[action])),
        // A failed claim has re-read the document already.
        after: (action) => {
          if (action !== 'claim') void reload();
        },
      },
    },
  };
};
