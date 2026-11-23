// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's direct edit of any processed document, in review or not: the shared
// editor over its sections, with the evaluation baseline where the document has one.
// Save writes the edits without reprocessing; Process Changes saves them, confirms,
// then submits them through processChanges, so evaluation and summarization re-run on
// them and OCR, classification and extraction are not repeated; Cancel leaves for the
// document's page. Edit Page Grouping saves a new grouping.
//
// A document in a review queue opens here read-only (for a role that does not work
// queues, or while the queue's mirror lags): it is edited from its queue item, and
// finalizing that item would overwrite edits made here. So does a document still
// processing. Save needs the Admin or Author role, or a Reviewer holding the document's
// legacy review; Process Changes needs an Admin, or the Reviewer who holds the document's
// open review. A document processed in BDA mode keeps its page grouping. The server
// enforces all of these.
import React, { useCallback } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { createIntl, defineMessages, FormattedMessage, useIntl, type IntlShape, type MessageTag, type NoMessageValues } from 'react-intl';
import { Link } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useConfiguration from '../../hooks/use-configuration';
import useUserRole from '../../hooks/use-user-role';
import { DOCUMENTS_PATH, MY_TASKS_PATH, documentEditHref, parseEditOrigin } from '../../routes/constants';
import { isBdaModeConfig } from '../../utils/processing-mode';
import { baselineCopyBlockedReason, copyDocumentToBaseline } from '../common/copy-to-baseline';
import { getConfigClassOptions } from '../common/config-class-options';
import { saveDocumentGroupingOrThrow } from '../common/document-grouping';
import { PROCESS_CHANGES_LABEL, sharedLabelMessages } from '../common/labels';
import { inReviewQueueEditReason, isInReviewQueue, normalizeHitlStatus } from '../common/review-status';
import type { GroupedSection } from '../common/section-grouping';
import { HOME_CRUMB, editActiveHref, editTrail } from '../review-queues/breadcrumbs';
import { documentName } from '../review-queues/review-api';
import { useDocumentEdit } from './document-edit-adapter';
import type { DirectEditDocument } from './document-edit-adapter';
import { BDA_REGROUP_REASON, documentEditStatusMessages, isEditableStatus } from './document-edit-status';
import { editPageErrorText, editScreenSharedMessages } from './edit-messages';
import type { EditModel, EditNavigation, ReviewActivity } from './edit-model';
import { documentMissingModel, loadingDocumentModel } from './edit-mode-states';
import type { DirectLock } from './edit-mode';
import type { RawDocument } from './use-edit-mode';
import type { EditorSession } from './use-editor-session';
import useSectionEvaluation from './use-section-evaluation';

type ProcessChanges = { readonly processChanges: string };

const messages = defineMessages<{
  processRoleReason: ProcessChanges;
  processOwnerReason: ProcessChanges;
  editRoleReason: NoMessageValues;
  regroupRoleReason: NoMessageValues;
  mirrorLagReason: NoMessageValues;
  mirrorLagNotice: NoMessageValues;
  inReviewNotice: NoMessageValues;
  processingNotice: NoMessageValues;
  viewOnly: { readonly reason: string };
  primaryHint: NoMessageValues;
  noSectionsToEdit: NoMessageValues;
  regroupConsequence: ProcessChanges & { readonly b: MessageTag };
  processing: { readonly name: string };
  baselineCopying: { readonly name: string };
  saved: { readonly name: string };
  failedProcess: { readonly error: string };
  failedBaseline: { readonly name: string; readonly error: string };
  failedReload: { readonly name: string; readonly error: string };
}>({
  processRoleReason: {
    id: 'editScreen.directEdit.processRoleReason',
    defaultMessage: '{processChanges} needs the Admin or Reviewer role.',
    description:
      "Why Process Changes is unavailable on a document's edit page: the user has neither the Admin nor the Reviewer role. {processChanges} is the action's name, Process Changes.",
  },
  processOwnerReason: {
    id: 'editScreen.directEdit.processOwnerReason',
    defaultMessage: "{processChanges} needs an Admin, or the reviewer who holds this document's open review. Start the review first.",
    description:
      "Why Process Changes is unavailable on a document's edit page for a Reviewer who does not hold the document's open review. {processChanges} is the action's name, Process Changes.",
  },
  editRoleReason: {
    id: 'editScreen.directEdit.editRoleReason',
    defaultMessage: "Editing needs the Admin or Author role, or a Reviewer who holds this document's review.",
    description:
      "Why a document's edit page is read-only, as the reason its actions are unavailable and in its read-only notice: the user is not an Admin or an Author, nor a Reviewer who holds the document's review.",
  },
  regroupRoleReason: {
    id: 'editScreen.directEdit.regroupRoleReason',
    defaultMessage: 'Edit Page Grouping needs the Admin or Reviewer role.',
    description: "Why Edit Page Grouping is unavailable on a document's edit page: the user has neither the Admin nor the Reviewer role.",
  },
  mirrorLagReason: {
    id: 'editScreen.directEdit.mirrorLagReason',
    defaultMessage: "This document's review is being updated. Reload in a moment.",
    description:
      "Why the actions of a document's edit page are unavailable while the document is marked as in a review queue but its review item is missing or has finished.",
  },
  mirrorLagNotice: {
    id: 'editScreen.directEdit.mirrorLagNotice',
    defaultMessage:
      'View only. This document is marked as in a review queue, but its review item is missing or has finished. Reload in a moment.',
    description: "Read-only notice at the top of a document's edit page in the case the sentence describes.",
  },
  inReviewNotice: {
    id: 'editScreen.directEdit.inReviewNotice',
    defaultMessage: 'This document is in a review queue. Edits are made in the queue.',
    description: "Read-only notice at the top of a document's edit page while the document is in a review queue.",
  },
  processingNotice: {
    id: 'editScreen.directEdit.processingNotice',
    defaultMessage: 'View only. This document is still processing; its results can be edited once processing finishes.',
    description: "Read-only notice at the top of a document's edit page while the document is still processing.",
  },
  viewOnly: {
    id: 'editScreen.directEdit.viewOnly',
    defaultMessage: 'View only. {reason}',
    description:
      "Read-only notice at the top of a document's edit page when the user's roles may not edit it. {reason} is a whole sentence saying which roles may.",
  },
  primaryHint: {
    id: 'editScreen.directEdit.primaryHint',
    defaultMessage:
      'Save your changes and process them: evaluation and summarization re-run; OCR, classification and extraction are not repeated.',
    description: "Hint of Process Changes in the action bar of a document's edit page: what it does.",
  },
  noSectionsToEdit: {
    id: 'editScreen.directEdit.noSectionsToEdit',
    defaultMessage: 'This document has no sections to edit.',
    description: "Shown in the section editor's place on a document's edit page when the document has no sections.",
  },
  regroupConsequence: {
    id: 'editScreen.directEdit.regroupConsequence',
    defaultMessage:
      "Moving pages rewrites this document's <b>section grouping</b>. The extracted field values are <b>kept</b> and the document is <b>not</b> reprocessed, so they may no longer match their pages. To have the pipeline redo them, open the document page's <b>More Actions › Edit Page Grouping</b> and choose <b>Save and Re-extract</b>: this screen's <b>{processChanges}</b> re-runs evaluation and summarization only.",
    description:
      "What saving a new page grouping does, in Edit Page Grouping opened from a document's edit page. <b>…</b> marks bold text. More Actions › Edit Page Grouping is a menu item and Save and Re-extract a button, both on the document's own page; {processChanges} is the edit page's action, Process Changes.",
  },
  processing: {
    id: 'editScreen.directEdit.processing',
    defaultMessage: 'Processing the changes to {name}: evaluation and summarization are re-running.',
    description: "Notice shown as a document's edit page closes after Process Changes was confirmed. {name} is the document's file name.",
  },
  baselineCopying: {
    id: 'editScreen.directEdit.baselineCopying',
    defaultMessage: 'Copying {name} to the evaluation baseline. Its evaluation status shows when the copy is done.',
    description:
      "Notice after Use As Evaluation Baseline on a document's edit page started copying the document to the evaluation baseline. {name} is the document's file name.",
  },
  saved: {
    id: 'editScreen.directEdit.saved',
    defaultMessage: 'Saved your changes to {name}.',
    description: "Notice after Save on a document's edit page saved the open section's changes. {name} is the document's file name.",
  },
  failedProcess: {
    id: 'editScreen.directEdit.failedProcess',
    defaultMessage: 'Could not process the changes: {error}',
    description:
      "Error on a document's edit page when Process Changes failed. {error} is why: the server's error text, shown as is, or, for an error the edit page raises itself, that error's own message in the user's language.",
  },
  failedBaseline: {
    id: 'editScreen.directEdit.failedBaseline',
    defaultMessage: 'Could not use {name} as the evaluation baseline: {error}',
    description:
      "Error on a document's edit page when Use As Evaluation Baseline failed. {name} is the document's file name; {error} is why: the server's error text, shown as is, or, for an error the edit page raises itself, that error's own message in the user's language.",
  },
  failedReload: {
    id: 'editScreen.directEdit.failedReload',
    defaultMessage: 'Could not reload {name}: {error}',
    description:
      "Error on a document's edit page when Refresh could not read the document again. {name} is the document's file name; {error} is why: the server's error text, shown as is, or, for an error the edit page raises itself, that error's own message in the user's language.",
  },
});

/** Words the English of the reasons below. */
const englishIntl = createIntl({ locale: 'en', defaultLocale: 'en' });

// The reasons in English: the direct edit's test compares with them.
export const PROCESS_OWNER_REASON = englishIntl.formatMessage(messages.processOwnerReason, { processChanges: PROCESS_CHANGES_LABEL });
export { BDA_REGROUP_REASON };
export const EDIT_ROLE_REASON = String(messages.editRoleReason.defaultMessage);
/** updateDocumentSections is Admin, Reviewer and Annotator; an Annotator regroups only test-set documents. */
export const REGROUP_ROLE_REASON = String(messages.regroupRoleReason.defaultMessage);
/** The tracking row says in review, but the review item is missing or has finished (the mirror lags). */
export const MIRROR_LAG_REASON = String(messages.mirrorLagReason.defaultMessage);

/** Renders a `<b>...</b>` element value. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

/** The legacy record of a whole-document skip; any other legacy record is one section's review. */
const SKIP_ALL_SECTION = 'ALL_SKIPPED';
const text = (v: unknown): string | undefined => (typeof v === 'string' && v ? v : undefined);

const historyMessages = defineMessages<{
  section: { readonly sectionId: string };
  notResubmitted: NoMessageValues;
  job: { readonly jobId: string };
}>({
  section: {
    id: 'editScreen.reviewActivity.section',
    defaultMessage: 'Section {sectionId}',
    description:
      'Part of a Review Activity note in the edit page History: the section the review event was for. {sectionId} is the section id, e.g. "2".',
  },
  notResubmitted: {
    id: 'editScreen.reviewActivity.notResubmitted',
    defaultMessage: 'Not resubmitted for downstream processing',
    description:
      'Part of a Review Activity note: Skip All Reviews ran without re-running Summarization, Evaluation and the post-processing hook.',
  },
  job: {
    id: 'editScreen.reviewActivity.job',
    defaultMessage: 'Job {jobId}',
    description: 'Part of a Review Activity note: the bulk job that made the review event. {jobId} is the job id.',
  },
});

/**
 * The document's HITL review events, in the shape History's Review Activity lists. The
 * legacy review writes `reviewedAt` and `reviewedBy`/`reviewedByEmail` (a finished section
 * has no `action`; Skip All Reviews is `skip_all`); `timestamp`, `user` and `action` are
 * read as well, for records in that shape.
 */
export const documentReviewHistory = (
  events: DirectEditDocument['hitlReviewHistory'],
  intl: IntlShape,
): { at: string; event: string; actor: string | null; details: string | null }[] =>
  (events ?? []).flatMap((e) => {
    const at = text(e.timestamp) ?? text(e.reviewedAt);
    const sectionId = text(e.sectionId);
    const event = text(e.action) ?? (sectionId ? 'section_reviewed' : undefined);
    if (!at || !event) return [];
    const reason = text(e.reason);
    const note = [
      text(e.note),
      sectionId && sectionId !== SKIP_ALL_SECTION ? intl.formatMessage(historyMessages.section, { sectionId }) : undefined,
      e.resubmitted === false ? intl.formatMessage(historyMessages.notResubmitted) : undefined,
      text(e.jobId) ? intl.formatMessage(historyMessages.job, { jobId: e.jobId as string }) : undefined,
    ]
      .filter(Boolean)
      .join(' · ');
    return [
      {
        at,
        event,
        actor: text(e.user) ?? text(e.reviewedByEmail) ?? text(e.reviewedBy) ?? null,
        details: reason || note ? JSON.stringify({ reason, note: note || undefined }) : null,
      },
    ];
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

export const useDirectEditModel = ({ objectKey, initialSectionId = null, seed }: DirectEditOptions, session: EditorSession): EditModel => {
  const intl = useIntl();
  const adapter = useDocumentEdit(objectKey, seed);
  const { document: doc, loading, loadError, reload } = adapter;
  const { user } = useAppContext();
  const { isAdmin, isReviewer, canWrite, isAnnotatorOnly } = useUserRole();
  const [searchParams] = useSearchParams();
  const location = useLocation();
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
  // The trail the user came by; without one, Tasks for an Annotator-only user, the document's page otherwise.
  const origin = parseEditOrigin(location.search) ?? (isAnnotatorOnly ? 'tasks' : 'document');
  const navigation: EditNavigation = { trail: editTrail({ origin, objectKey, name }), activeHref: editActiveHref(origin) };
  // Home only until the document is known.
  const unloaded: EditNavigation = { trail: [HOME_CRUMB], activeHref: null };

  if (loading && !doc) return loadingDocumentModel(intl);
  if (!doc) {
    if (loadError === 'NOT_FOUND') return documentMissingModel(isAnnotatorOnly, intl);
    return {
      navigation: unloaded,
      state: {
        kind: 'failure',
        type: 'error',
        header: intl.formatMessage(editScreenSharedMessages.couldNotOpenDocument),
        body: (
          <>
            {loadError === null || loadError === 'load-failed' ? (
              <FormattedMessage {...editScreenSharedMessages.documentCouldNotLoad} />
            ) : (
              loadError
            )}{' '}
            {isAnnotatorOnly ? (
              <Link href={`#${MY_TASKS_PATH}`}>
                <FormattedMessage {...editScreenSharedMessages.backToTasks} />
              </Link>
            ) : (
              <Link href={`#${DOCUMENTS_PATH}`}>
                <FormattedMessage {...editScreenSharedMessages.backToDocuments} />
              </Link>
            )}
          </>
        ),
        retry: () => void reload(),
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
  const processingReason = intl.formatMessage(documentEditStatusMessages.processing);
  const savingChanges = intl.formatMessage(editScreenSharedMessages.savingChanges);
  const processChanges = intl.formatMessage(sharedLabelMessages.processChanges);

  // Why the document is read-only, if it is.
  let readOnlyCause: 'in-review' | 'processing' | 'edit-role' | undefined;
  if (inReview) readOnlyCause = 'in-review';
  else if (!isEditableStatus(status)) readOnlyCause = 'processing';
  else if (!canWrite && !holdsReview) readOnlyCause = 'edit-role';
  let readOnlyReason: string | undefined;
  if (readOnlyCause === 'in-review')
    readOnlyReason = lock === 'mirror-lag' ? intl.formatMessage(messages.mirrorLagReason) : inReviewQueueEditReason(false, intl);
  else if (readOnlyCause === 'processing') readOnlyReason = processingReason;
  else if (readOnlyCause === 'edit-role') readOnlyReason = intl.formatMessage(messages.editRoleReason);
  // Summary and Use as Evaluation Baseline are about a finished document: not one still
  // processing, nor one in a review queue, whose results change when its review ends.
  const finished = !inReview && isEditableStatus(status);
  // Why they are unavailable, for the menus (an item that does not apply stays, disabled).
  const unfinishedReason = inReview
    ? intl.formatMessage(documentEditStatusMessages.inReviewShort)
    : !isEditableStatus(status)
      ? processingReason
      : undefined;
  const readOnly = readOnlyReason !== undefined;
  // The same reason, said in full for the page's read-only notice.
  let readOnlyNotice: string | undefined;
  if (readOnlyCause === 'in-review')
    readOnlyNotice = intl.formatMessage(lock === 'mirror-lag' ? messages.mirrorLagNotice : messages.inReviewNotice);
  else if (readOnlyCause === 'processing') readOnlyNotice = intl.formatMessage(messages.processingNotice);
  else if (readOnlyCause === 'edit-role')
    readOnlyNotice = intl.formatMessage(messages.viewOnly, { reason: intl.formatMessage(messages.editRoleReason) });
  // In review, the editing actions stay on the bar, disabled with the in-review reason.
  const showEditing = !readOnly || inReview;
  const { dirty, saving } = session;

  let processReason: string | undefined = readOnlyReason;
  if (!processReason && !(isAdmin || isReviewer)) processReason = intl.formatMessage(messages.processRoleReason, { processChanges });
  // The server's rule for processChanges: a caller who is not an Admin must hold the
  // document's OPEN review (pending or in progress). Holding a finished one is not
  // enough, and neither is the Author role a Reviewer may also have.
  const reviewOpen = ['pending', 'in_progress'].includes(normalizeHitlStatus(doc.hitlStatus).kind);
  if (!processReason && !isAdmin && !(holdsReview && reviewOpen))
    processReason = intl.formatMessage(messages.processOwnerReason, { processChanges });
  if (!processReason && saving) processReason = savingChanges;

  // From the document's own configuration, not the stack's `IDPPattern` (which names
  // the one unified pattern for every document). In BDA mode the page grouping is not
  // offered: a BDA reprocess keeps the sections a document already has.
  const bdaMode = isBdaModeConfig(mergedConfig);

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
          unavailableReason: unfinishedReason ?? intl.formatMessage(documentEditStatusMessages.noSummary),
        },
        offerReport: true,
      },
      notices: [],
      bar: {
        showSave: showEditing,
        saveDisabledReason:
          readOnlyReason ?? (!dirty ? intl.formatMessage(editScreenSharedMessages.noChangesToSave) : saving ? savingChanges : undefined),
        // Listed for everyone, disabled with the read-only reason where it does not apply.
        showRegroup: true,
        regroupDisabledReason:
          readOnlyReason ??
          (!(isAdmin || isReviewer) ? intl.formatMessage(messages.regroupRoleReason) : undefined) ??
          (bdaMode ? intl.formatMessage(documentEditStatusMessages.bdaRegroup) : undefined) ??
          (boardSections.length === 0 ? intl.formatMessage(documentEditStatusMessages.noSections) : undefined),
        // copyToBaseline is Admin and Author (canWrite), as on the document page.
        showBaseline: canWrite,
        baselineDisabledReason: unfinishedReason ?? baselineCopyBlockedReason(doc.evaluationStatus, intl),
        showCancel: !readOnly,
        cancelDisabledReason: saving ? savingChanges : undefined,
        showRefresh: true,
        refreshDisabledReason: saving ? savingChanges : undefined,
        primaryDisabledReason: processReason,
        primaryHint: intl.formatMessage(messages.primaryHint),
        offerHistory: true,
      },
      editor: {
        sections: adapter.sections,
        pages: adapter.pages,
        readOnly,
        readOnlyReason: readOnlyNotice,
        loadSection: adapter.loadSection,
        saveSection: onSave,
        initialSectionId,
        focusFieldPath: focusField,
        buildFieldLink,
        mergedConfig: mergedConfig ?? undefined,
        evaluation,
        emptyText: intl.formatMessage(messages.noSectionsToEdit),
      },
      board: {
        pages: { kind: 'thumbnails', pages: adapter.pages },
        sections: () => boardSections,
        classOptions: getConfigClassOptions(mergedConfig),
        canChangeClass: true,
        consequence: <FormattedMessage {...messages.regroupConsequence} values={{ b: bold, processChanges }} />,
        save: async (next) => {
          const message = await saveDocumentGroupingOrThrow(objectKey, next);
          await reload();
          return message;
        },
      },
      // A finished queue review's activity, then the document's own review events.
      history: {
        reviewActivity: [...(seed?.reviewActivity ?? []), ...documentReviewHistory(doc.hitlReviewHistory, intl)],
        baseline: evaluation.baseline,
      },
      process: {
        confirm: { kind: 'reprocess', bdaMode },
        run: async () => {
          await adapter.processChanges();
          return { to: leaveTo, message: intl.formatMessage(messages.processing, { name }) };
        },
      },
      baseline: async () => {
        await copyDocumentToBaseline(objectKey);
        void reload();
        return intl.formatMessage(messages.baselineCopying, { name });
      },
      savedMessage: intl.formatMessage(messages.saved, { name }),
      refresh: adapter.refresh,
      cancelTo: leaveTo,
      failures: {
        surface: 'notice',
        describe: (action, err) => {
          const error = editPageErrorText(err, intl);
          if (action === 'process') return intl.formatMessage(messages.failedProcess, { error });
          if (action === 'baseline') return intl.formatMessage(messages.failedBaseline, { name, error });
          if (action === 'refresh') return intl.formatMessage(messages.failedReload, { name, error });
          return error;
        },
      },
    },
  };
};
