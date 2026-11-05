// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Pure decisions behind the edit screen and its ground-truth editor: who may edit
 * what, which save path is used, the version transition a session belongs to, and the
 * copy that explains each state.
 *
 * Kept out of the components so each rule is tested as a function rather than by
 * pattern-matching the component source, and so the edit screen can reuse
 * them without importing a page.
 */

import { PROCESS_CHANGES_LABEL } from '../common/labels';

/** The role flags these decisions read, as `useUserRole` reports them. */
export interface AnnotationRoles {
  isAdmin: boolean;
  isAuthor: boolean;
  isReviewer: boolean;
  isAnnotator: boolean;
  canAnnotate: boolean;
}

/**
 * The version transition an annotation session is working within.
 *
 * `baseVersion` is the state being left. Publishing it copied its labels to
 * `{testSetId}/versions/{baseVersion}/baseline/`, so they are already safe from this
 * session; `draftVersion` is what this session is producing. Carried in the queue link so a
 * link identifies its own transition.
 */
export interface AnnotationDraft {
  baseVersion: number;
  draftVersion: number;
  snapshotObjectCount?: number | null;
  alreadyOpen?: boolean | null;
}

/** The part of a queue document these decisions read. */
export interface AnnotationSelection {
  reviewObjectKey?: string | null;
  claimedByMe: boolean;
  reviewed: boolean;
}

/**
 * Who the SERVER will accept, per save path — the two differ, and neither matches
 * `canAnnotate`.
 *
 * A document with a review record saves through `completeSectionReview`
 * (`Admin, Annotator` on a test-set document; its Reviewer group covers Production
 * documents only). One without saves through `saveTestSetDocumentSection`
 * (`Admin, Author, Annotator`). So `Author` is refused the first. A Reviewer works
 * Production queues only and is refused both.
 *
 * Gating on `canAnnotate` (Admin | Author | Annotator) offered editing to users the
 * server would refuse: an Annotator would have corrected a class and lost it to an
 * authorization error at save. The server is still what enforces this; these booleans
 * exist so the UI does not invite work it cannot persist.
 */
export const saveCapabilities = (
  roles: AnnotationRoles,
): { canSaveViaReview: boolean; canSaveDirectToBaseline: boolean; canDraft: boolean } => ({
  // completeSectionReview also admits a Reviewer, but only on Production documents: the
  // server refuses a Reviewer-only caller on a test-set document.
  canSaveViaReview: roles.isAdmin || roles.isAnnotator,
  // saveTestSetDocumentSection: an Annotator saves only documents assigned to them.
  canSaveDirectToBaseline: roles.isAdmin || roles.isAuthor || roles.isAnnotator,
  // saveTestSetDocumentSectionDraft: an Author only on authored ground truth.
  canDraft: roles.isAdmin || roles.isAuthor || roles.isAnnotator,
});

export interface AnnotationEditFlags {
  /** Field edits are refused: the server would refuse this save path for these roles. */
  isReadOnly: boolean;
  /** Save through `completeSectionReview` (true) or the editor's direct write (false). */
  saveViaReview: boolean;
  /**
   * Wider than `!isReadOnly` on purpose. A class correction persists via
   * `reextractTestSetDocument` (Admin, Author, Annotator), which stamps the baseline
   * server-side and needs no review record — so every role that may work this queue may
   * also correct a class, even on a document whose FIELD edits they could not save.
   */
  canChangeClass: boolean;
  canSaveViaReview: boolean;
  canSaveDirectToBaseline: boolean;
}

/**
 * The editor's gating for one selected document.
 *
 * Draft-first: fields are editable whether or not a draft version is open, and Save keeps
 * the edits as a draft without opening one. Process Changes is what opens the next version,
 * behind a confirmation, so the commitment is explicit without blocking the edits before
 * it. Looking is always allowed.
 *
 * The read-only half is chosen per save path, by role. It used to be `!canAnnotate ||
 * !selected.reviewObjectKey`, so a missing review record forced read-only whatever the
 * role: an Admin opening a document that already carried authored ground truth got a
 * disabled class dropdown under an alert promising they could correct the values
 * ([#674]). The review record gates the review WORKFLOW — claim, release, mark reviewed —
 * not editing.
 *
 * The review API is used only when there IS a review to complete:
 * `completeSectionReview` requires `reviewObjectKey`, so leaving it wired for an
 * authored-ground-truth document would lose the edit at save time.
 */
export const annotationEditFlags = (
  roles: AnnotationRoles,
  selected: Pick<AnnotationSelection, 'reviewObjectKey'>,
): AnnotationEditFlags => {
  const { canSaveViaReview, canSaveDirectToBaseline } = saveCapabilities(roles);
  const saveViaReview = Boolean(selected.reviewObjectKey);
  // A review copy is edited (saved as a draft, then completed) by an Admin or an
  // Annotator: completion has no Author, and a Reviewer does not work test sets.
  const mayEditReviewCopy = canSaveViaReview && (roles.isAdmin || roles.isAnnotator);
  return {
    isReadOnly: saveViaReview ? !mayEditReviewCopy : !canSaveDirectToBaseline,
    saveViaReview,
    canChangeClass: roles.canAnnotate,
    canSaveViaReview,
    canSaveDirectToBaseline,
  };
};

export const NO_REVIEW_RECORD_MESSAGE = 'This document has no review record yet — generate draft labels for the test set first.';

/**
 * The review-save backstop: refuse to call `completeSectionReview` for a document with
 * no review record. The editor's save routing already avoids it; this is what still
 * holds if that routing is ever changed back.
 */
export const requireReviewObjectKey = (selected: Pick<AnnotationSelection, 'reviewObjectKey'> | null): string => {
  if (!selected?.reviewObjectKey) {
    throw new Error(NO_REVIEW_RECORD_MESSAGE);
  }
  return selected.reviewObjectKey;
};

/**
 * The open transition, preferring what the server reported on the queue.
 *
 * Read from `getAnnotationQueue` rather than probed with the mutation: opening a draft
 * records the transition, and publishes an unpublished set's arriving labels, so using it
 * to *find out* whether one exists would commit to a transition merely by visiting the
 * page. `openedDraft` covers the gap between Process Changes opening the version and the
 * next queue refresh returning the same numbers.
 */
export const currentDraft = (
  queue: { draftVersion?: number | null; baseVersion?: number | null } | null,
  openedDraft: AnnotationDraft | null,
): AnnotationDraft | null =>
  queue?.draftVersion != null
    ? { baseVersion: queue.baseVersion ?? queue.draftVersion - 1, draftVersion: queue.draftVersion }
    : openedDraft;

/**
 * How precisely the accuracy estimate may be printed. A tenth of a percent is a claim
 * about the second significant figure, which only a curve measured on this set supports;
 * below that the number moves by whole points per review. Applied to the headline, the
 * projection under it and the quality tier beside them, so the three read as one number.
 */
export const estimateDecimals = (estimateConfidence: string | null | undefined): number => (estimateConfidence === 'measured' ? 1 : 0);

/**
 * `decimals` carries how much precision the number has earned. EstimateConfidence
 * exists "so a cold-start estimate is never rendered with the same authority as a
 * measured one" — one review of a 12-document set moved a displayed 85.1% to 92.2%, a
 * swing two orders of magnitude wider than the digit being shown.
 */
export const formatPct = (fraction: number, decimals = 1): string =>
  Number.isFinite(fraction) ? `${(fraction * 100).toFixed(decimals)}%` : '—';

/**
 * The queue link, carrying the transition it belongs to.
 *
 * Without `?v=`, a shared link means "annotate this set" — which silently becomes a
 * different job once the current transition is published and the next one opens. No
 * `?v=` before a draft exists, because there is no transition yet to name.
 */
export const annotationQueueLink = (origin: string, annotateHref: string, draft: AnnotationDraft | null): string =>
  draft ? `${origin}/${annotateHref}?v=${draft.draftVersion}` : `${origin}/${annotateHref}`;

/**
 * A link from a transition that has since closed, or null.
 *
 * Compared against the open draft, not merely "is a version present"; and only an
 * integer names a version — `?v=abc` used to warn about "version abc, which is closed".
 */
export const staleLinkVersion = (requestedVersion: string | null, draft: AnnotationDraft | null): number | null => {
  const requested = requestedVersion && Number.isInteger(Number(requestedVersion)) ? Number(requestedVersion) : null;
  return draft && requested !== null && requested !== draft.draftVersion ? requested : null;
};

/** The `vN → vM` badge of an open transition, on the queue page and in the edit header. */
export const versionBadgeText = (draft: AnnotationDraft): string => `v${draft.baseVersion} → v${draft.draftVersion}`;

/** Hover text for the `vN → vM` badge: where corrections go while a draft version is open. */
export const draftBadgeHover = (draft: AnnotationDraft): string =>
  `Corrections go into version v${draft.draftVersion}; v${draft.baseVersion} is preserved.`;

/**
 * The edit screen's prompt while no draft version is open: what each step writes. No
 * version numbers, because the queue reports none until a draft version is open.
 */
export const DRAFT_PROMPT_HEADER = 'Save keeps a draft';
export const DRAFT_PROMPT_BODY = `${PROCESS_CHANGES_LABEL} opens the next version of this set (the current version stays as it is) and writes these labels as reviewed ground truth. Publish version on the test set freezes it.`;

/**
 * The closing sentence of the "Already ground truth" alert, by whether this user may save
 * it. The direct save counts as a review, so copy denying that would be false.
 */
export const groundTruthAlertSuffix = (canSaveDirectToBaseline: boolean): string =>
  canSaveDirectToBaseline
    ? ` Save keeps your changes; ${PROCESS_CHANGES_LABEL} writes the ground truth and counts the document as reviewed.`
    : ' Only its assignee, an Admin or an Author can change it — you can inspect the values below.';

/**
 * The queue rail's empty text. "No documents to review" is wrong when a filter is what
 * emptied the list — the documents are there, the text just does not match one.
 */
export const queueEmptyText = (queueFilter: string): string =>
  queueFilter ? `No document matches "${queueFilter}".` : 'No documents to review.';

/** Why a search can come up empty: the likeliest reason is hidden by default. */
export const REVIEWED_HIDDEN_HINT = 'Reviewed documents are hidden — show them to search those too.';

/**
 * Whether the editor may change the CLASS. Defaults to `!isReadOnly`, so a caller that
 * does not distinguish the two capabilities keeps the field-edit behaviour.
 */
export const mayChangeClassFor = (canChangeClass: boolean | undefined, isReadOnly: boolean): boolean => canChangeClass ?? !isReadOnly;

/**
 * True when the class list could not be read, as opposed to being genuinely empty. A
 * denied or failed fetch must not be mistaken for a config that defines no classes: the
 * free-text fallback would hand the role most in need of a constrained vocabulary an
 * unconstrained box, and a typed class no config defines extracts nothing.
 */
export const isClassListUnavailable = (classOptionCount: number, configError: unknown, configLoading: boolean): boolean =>
  classOptionCount === 0 && (Boolean(configError) || configLoading);

export const CLASS_LIST_UNAVAILABLE_DESCRIPTION =
  'What this section is classified as. The list of valid classes could not be loaded, so it cannot be changed here.';
export const CLASS_LIST_UNAVAILABLE_CONSTRAINT =
  'Your role cannot read the configuration profile this set was labelled with, so the valid classes are unknown. An Admin or Author can change the class.';
