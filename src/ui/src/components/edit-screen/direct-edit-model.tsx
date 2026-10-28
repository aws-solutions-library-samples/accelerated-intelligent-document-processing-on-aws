// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's direct edit of any processed document, in review or not: the shared
// editor over its sections, with the evaluation baseline where the document has one.
// Save writes the edits without reprocessing; Process Changes saves them, confirms,
// then submits them through processChanges, so evaluation and summarization re-run on
// them and OCR, classification and extraction are not repeated; Cancel leaves for the
// document's page. Edit Page Class saves a new grouping.
//
// A document in a review queue opens here read-only (for a role that does not work
// queues, or while the queue's mirror lags): it is edited from its queue item, and
// finalizing that item would overwrite edits made here. So does a document still
// processing. Save needs the Admin or Author role, or a Reviewer holding the document's
// legacy review; Process Changes needs the Admin or Reviewer role. The server enforces
// both.
import React, { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Link } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useConfiguration from '../../hooks/use-configuration';
import useUserRole from '../../hooks/use-user-role';
import { DOCUMENTS_PATH, MY_TASKS_PATH, documentDetailHref, documentEditHref } from '../../routes/constants';
import { getErrorMessage } from '../../utils/errorUtils';
import { baselineCopyBlockedReason, copyDocumentToBaseline } from '../common/copy-to-baseline';
import { getConfigClassOptions } from '../common/config-class-options';
import { saveDocumentGrouping } from '../common/document-grouping';
import { inReviewQueueEditReason, isInReviewQueue } from '../common/review-status';
import type { GroupedSection } from '../common/section-grouping';
import { documentName } from '../review-queues/review-api';
import { useDocumentEdit } from './document-edit-adapter';
import type { DirectEditDocument } from './document-edit-adapter';
import { PROCESSING_REASON, isEditableStatus } from './document-edit-status';
import type { EditModel, EditNavigation, ReviewActivity } from './edit-model';
import type { DirectLock } from './edit-mode';
import type { RawDocument } from './use-edit-mode';
import type { EditorSession } from './use-editor-session';
import useSectionEvaluation from './use-section-evaluation';

export const PROCESS_ROLE_REASON = 'Process Changes needs the Admin or Reviewer role.';
export const EDIT_ROLE_REASON = "Editing needs the Admin or Author role, or a Reviewer who holds this document's review.";
/** updateDocumentSections is Admin, Reviewer and Annotator; an Annotator regroups only test-set documents. */
export const REGROUP_ROLE_REASON = 'Edit Page Class needs the Admin or Reviewer role.';
/** The tracking row says in review, but the review item is missing or has finished (the mirror lags). */
export const MIRROR_LAG_REASON = "This document's review is being updated. Reload in a moment.";

/** The document's HITL review events, in the shape History's Review Activity lists. */
export const documentReviewHistory = (
  events: DirectEditDocument['hitlReviewHistory'],
): { at: string; event: string; actor: string | null; details: string | null }[] =>
  (events ?? [])
    .filter((e) => !!e.timestamp && !!e.action)
    .map((e) => {
      const reason = typeof e.reason === 'string' ? e.reason : undefined;
      const note = [typeof e.note === 'string' ? e.note : undefined, e.sectionId ? `Section ${e.sectionId}` : undefined]
        .filter(Boolean)
        .join(' · ');
      return {
        at: e.timestamp as string,
        event: e.action as string,
        actor: e.user ?? null,
        details: reason || note ? JSON.stringify({ reason, note: note || undefined }) : null,
      };
    });

export interface DirectEditOptions {
  objectKey: string;
  /** The section to open first (from `?section=`). */
  initialSectionId?: string | null;
  /**
   * What the edit page read to choose this mode: the document, why it is read-only when
   * it is in review, and a finished queue review's activity, for History.
   */
  seed?: { document: RawDocument; lock?: DirectLock; reviewActivity?: ReviewActivity };
}

const TASKS_CRUMB = { text: 'Tasks', href: `#${MY_TASKS_PATH}` };

export const useDirectEditModel = ({ objectKey, initialSectionId = null, seed }: DirectEditOptions, session: EditorSession): EditModel => {
  const adapter = useDocumentEdit(objectKey, seed);
  const { document: doc, loading, loadError, reload } = adapter;
  const { user } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, isReviewer, canWrite, isAnnotatorOnly } = useUserRole();
  const [searchParams] = useSearchParams();
  const focusField = searchParams.get('field');
  const { mergedConfig } = useConfiguration(doc?.configVersion || 'default');
  const selectedSection = adapter.sections.find((s) => s.sectionId === session.selectedSectionId) ?? null;
  const evaluation = useSectionEvaluation({
    enabled: !!doc,
    objectKey,
    outputUri: selectedSection?.resultUri || null,
    evaluationStatus: doc?.evaluationStatus,
    reviewerOnly: adapter.reviewerOnly,
  });
  const buildFieldLink = useCallback(
    (fieldPath: string) => `${window.location.origin}/${documentEditHref(objectKey, { field: fieldPath })}`,
    [objectKey],
  );
  const onSave = useCallback(
    (sectionId: string, data: Record<string, unknown>) => adapter.saveSection(sectionId, data, evaluation),
    [adapter.saveSection, evaluation],
  );

  const name = documentName(objectKey);
  // The document's page: where Cancel and Process Changes leave to; an Annotator-only
  // user, who has no Documents, goes back to Tasks.
  const leaveTo = isAnnotatorOnly ? MY_TASKS_PATH : `${DOCUMENTS_PATH}/${encodeURIComponent(objectKey)}`;
  const navigation: EditNavigation = isAnnotatorOnly
    ? { root: TASKS_CRUMB, crumbs: [{ text: name, href: '#' }], activeHref: `#${MY_TASKS_PATH}` }
    : {
        root: { text: 'Documents', href: `#${DOCUMENTS_PATH}` },
        crumbs: [{ text: name, href: documentDetailHref(objectKey) }],
        activeHref: `#${DOCUMENTS_PATH}`,
      };
  // Home only until the document is known.
  const unloaded: EditNavigation = { root: null, crumbs: [], activeHref: null };

  if (loading && !doc) return { navigation: unloaded, state: { kind: 'loading', text: 'Loading document…' } };
  if (!doc) {
    const notFound = loadError === 'NOT_FOUND';
    return {
      navigation: unloaded,
      state: {
        kind: 'failure',
        type: notFound ? 'warning' : 'error',
        header: notFound ? 'Document not found' : "Couldn't open this document",
        body: (
          <>
            {notFound ? 'This document no longer exists. ' : `${loadError ?? 'The document could not be loaded.'} `}
            {isAnnotatorOnly ? (
              <Link href={`#${MY_TASKS_PATH}`}>Back to Tasks</Link>
            ) : (
              <Link href={`#${DOCUMENTS_PATH}`}>Back to Documents</Link>
            )}
          </>
        ),
        retry: notFound ? undefined : () => void reload(),
      },
    };
  }

  const status = doc.objectStatus ?? '';
  const loginId = (user?.signInDetails?.loginId ?? '').toLowerCase();
  const holdsReview =
    isReviewer &&
    ((!!doc.hitlReviewOwner && doc.hitlReviewOwner === user?.username) ||
      (!!doc.hitlReviewOwnerEmail && doc.hitlReviewOwnerEmail.toLowerCase() === loginId));
  // In review: the page chose this mode because the caller does not work queues, or the
  // queue's mirror lags. Either way it is edited in the queue, not here.
  const lock = seed?.lock;
  const inReview = !!lock || isInReviewQueue(doc.hitlStatus);
  const inReviewReason = lock === 'mirror-lag' ? MIRROR_LAG_REASON : inReviewQueueEditReason(false);

  let readOnlyReason: string | undefined;
  if (inReview) readOnlyReason = inReviewReason;
  else if (!isEditableStatus(status)) readOnlyReason = PROCESSING_REASON;
  else if (!canWrite && !holdsReview) readOnlyReason = EDIT_ROLE_REASON;
  // Summary and Use as Evaluation Baseline are about a finished document: not one still
  // processing, nor one in a review queue, whose results change when its review ends.
  const finished = !inReview && isEditableStatus(status);
  // Why they are unavailable, for the menus (an item that does not apply stays, disabled).
  const unfinishedReason = inReview
    ? 'This document is in a review queue'
    : !isEditableStatus(status)
      ? 'Available when processing finishes'
      : undefined;
  const readOnly = readOnlyReason !== undefined;
  // In review, the editing actions stay on the bar, disabled with the in-review reason.
  const showEditing = !readOnly || inReview;
  const { dirty, saving } = session;

  let processReason: string | undefined = readOnlyReason;
  if (!processReason && !(isAdmin || isReviewer)) processReason = PROCESS_ROLE_REASON;
  if (!processReason && saving) processReason = 'Saving your changes…';

  const bdaMode = String((settings as Record<string, unknown>)?.IDPPattern ?? '')
    .toLowerCase()
    .includes('pattern1');

  const boardSections: GroupedSection[] = (doc.sections ?? []).map((s) => ({
    sectionId: String(s.Id),
    documentClass: s.Class ?? null,
    pageIds: (s.PageIds ?? []).map((id) => Number(id)),
  }));

  return {
    navigation,
    state: {
      kind: 'ready',
      header: {
        title: name,
        status,
        objectKey,
        source: { objectKey },
        summary: {
          reportUri: finished ? doc.summaryReportUri || undefined : undefined,
          unavailableReason: unfinishedReason ?? 'This document has no summary',
        },
        offerReport: true,
      },
      notices: [
        inReview && {
          id: 'in-review',
          type: 'info',
          content:
            lock === 'mirror-lag'
              ? 'View only. This document is marked as in a review queue, but its review item is missing or has finished. Reload in a moment.'
              : 'This document is in a review queue. Edits are made in the queue.',
        },
        !inReview &&
          readOnlyReason === PROCESSING_REASON && {
            id: 'processing',
            type: 'info',
            content: 'View only. This document is still processing; its results can be edited once processing finishes.',
          },
        !inReview && readOnlyReason === EDIT_ROLE_REASON && { id: 'role', type: 'info', content: `View only. ${EDIT_ROLE_REASON}` },
      ],
      bar: {
        showSave: showEditing,
        saveDisabledReason: readOnlyReason ?? (!dirty ? 'No changes to save' : saving ? 'Saving your changes…' : undefined),
        // Listed for everyone, disabled with the read-only reason where it does not apply.
        showRegroup: true,
        regroupDisabledReason:
          readOnlyReason ??
          (!(isAdmin || isReviewer) ? REGROUP_ROLE_REASON : undefined) ??
          (boardSections.length === 0 ? 'This document has no sections' : undefined),
        // copyToBaseline is Admin and Author (canWrite), as on the document page.
        showBaseline: canWrite,
        baselineDisabledReason: unfinishedReason ?? baselineCopyBlockedReason(doc.evaluationStatus),
        showCancel: !readOnly,
        cancelDisabledReason: saving ? 'Saving your changes…' : undefined,
        primaryDisabledReason: processReason,
        primaryHint:
          'Save your changes and process them: evaluation and summarization re-run; OCR, classification and extraction are not repeated.',
        offerHistory: true,
      },
      editor: {
        sections: adapter.sections,
        pages: adapter.pages,
        readOnly,
        loadSection: adapter.loadSection,
        saveSection: onSave,
        initialSectionId,
        focusFieldPath: focusField,
        buildFieldLink,
        mergedConfig: mergedConfig ?? undefined,
        evaluation,
        emptyText: 'This document has no sections to edit.',
      },
      board: {
        pages: { kind: 'thumbnails', pages: adapter.pages },
        sections: () => boardSections,
        classOptions: getConfigClassOptions(mergedConfig),
        canChangeClass: true,
        consequence: (
          <>
            Moving pages rewrites this document&apos;s <b>section grouping</b>. The extracted field values are <b>kept</b> and the document
            is <b>not</b> reprocessed, so they may no longer match their pages. To have the pipeline redo them, use the document page&apos;s{' '}
            <b>Edit Mode → Process Changes</b> afterwards: this screen&apos;s <b>Process Changes</b> re-runs evaluation and summarization
            only.
          </>
        ),
        save: async (next) => {
          let result;
          try {
            result = await saveDocumentGrouping(objectKey, next);
          } catch (err) {
            throw new Error(`Could not save the page grouping: ${getErrorMessage(err)}`);
          }
          if (!result.success) throw new Error(result.message ?? 'The page grouping could not be saved.');
          await reload();
          return result.message ?? null;
        },
      },
      // A finished queue review's activity, then the document's own review events.
      history: {
        reviewActivity: [...(seed?.reviewActivity ?? []), ...documentReviewHistory(doc.hitlReviewHistory)],
        baseline: evaluation.baseline,
      },
      process: {
        confirm: { bdaMode },
        run: async () => {
          await adapter.processChanges();
          return { to: leaveTo, message: `Processing the changes to ${name}: evaluation and summarization are re-running.` };
        },
      },
      baseline: async () => {
        await copyDocumentToBaseline(objectKey);
        void reload();
        return `Copying ${name} to the evaluation baseline. Its evaluation status shows when the copy is done.`;
      },
      savedMessage: `Saved your changes to ${name}.`,
      cancelTo: leaveTo,
      failures: {
        surface: 'notice',
        describe: (action, err) => {
          if (action === 'process') return `Could not process the changes: ${getErrorMessage(err)}`;
          if (action === 'baseline') return `Could not use ${name} as the evaluation baseline: ${getErrorMessage(err)}`;
          return getErrorMessage(err);
        },
      },
    },
  };
};
