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
 * (`Admin, Reviewer, Annotator`). One without saves through the editor's direct-to-S3
 * write, which needs `uploadDocument` (`Admin, Author`). So `Author` is refused the
 * first and `Annotator` the second.
 *
 * Gating on `canAnnotate` (Admin | Author | Annotator) offered editing to users the
 * server would refuse: an Annotator would have corrected a class and lost it to an
 * authorization error at save. The server is still what enforces this; these booleans
 * exist so the UI does not invite work it cannot persist.
 */
export const saveCapabilities = (roles: AnnotationRoles): { canSaveViaReview: boolean; canSaveDirectToBaseline: boolean } => ({
  canSaveViaReview: roles.isAdmin || roles.isReviewer || roles.isAnnotator,
  // saveTestSetDocumentSection: an Annotator saves only documents assigned to them.
  canSaveDirectToBaseline: roles.isAdmin || roles.isAuthor || roles.isAnnotator,
});

export interface AnnotationEditFlags {
  /** Field edits are refused: no open transition, or the server would refuse this save path. */
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
 * Read-only until the transition is open, as well as by role: letting someone edit first
 * and open the version afterwards would put the commitment back where it was — implicit,
 * and discovered only once the labels had already changed. Looking is always allowed.
 *
 * The role half is chosen per save path. It used to be `!canAnnotate ||
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
  draft: AnnotationDraft | null,
): AnnotationEditFlags => {
  const { canSaveViaReview, canSaveDirectToBaseline } = saveCapabilities(roles);
  const saveViaReview = Boolean(selected.reviewObjectKey);
  return {
    isReadOnly: !draft || (saveViaReview ? !canSaveViaReview : !canSaveDirectToBaseline),
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
 * page. `openedDraft` covers the gap between clicking Start annotating and the next queue
 * refresh returning the same numbers.
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

/** The confirmation shown when a transition opens, naming what the server preserved. */
export const draftOpenedMessage = (opened: AnnotationDraft): string =>
  opened.alreadyOpen
    ? `Version ${opened.draftVersion} was already open on this set; your corrections continue into it.`
    : `Version ${opened.baseVersion} preserved (${(opened.snapshotObjectCount ?? 0).toLocaleString()} objects). Your corrections go into version ${opened.draftVersion}.`;

/** Hover text for the `vN → vM` badge, for a reader who did not open the transition. */
export const draftBadgeHover = (draft: AnnotationDraft): string =>
  `Corrections made here go into version ${draft.draftVersion}. Version ${draft.baseVersion}, the labels this set had before, is preserved and can still be scored against.`;

export const DRAFT_PROMPT_HEADER = 'Editing this set creates a new version of it';
export const DRAFT_PROMPT_BODY =
  'Its current labels are preserved as a version you can go back to and score against, so a run that was measured against them stays reproducible. Your corrections go into the next version.';

/**
 * The closing sentence of the "Already ground truth" alert.
 *
 * Three states, not two. Before a transition is open the editor is read-only, so
 * promising "you can still correct it below" contradicted the alert above it; and the
 * direct save counts as a review, so copy denying that was false.
 */
export const groundTruthAlertSuffix = (canSaveDirectToBaseline: boolean, draft: AnnotationDraft | null): string =>
  canSaveDirectToBaseline
    ? draft
      ? ' Saving a section, or Complete, writes the ground truth and counts the document as reviewed.'
      : ' Use Start annotating, above, to correct it.'
    : ' Only its assignee, an Admin or an Author can change it — you can inspect the values below.';

/**
 * The queue rail's empty text. "No documents to review" is wrong when a filter is what
 * emptied the list — the documents are there, the text just does not match one.
 */
export const queueEmptyText = (queueFilter: string): string =>
  queueFilter ? `No document matches "${queueFilter}".` : 'No documents to review.';

/** Why a search can come up empty: the likeliest reason is hidden by default. */
export const REVIEWED_HIDDEN_HINT = 'Reviewed documents are hidden — show them to search those too.';

export interface DocumentActionState {
  claimVariant: 'primary' | 'normal';
  confirmVariant: 'primary' | 'normal';
  confirmDisabled: boolean;
}

/**
 * The per-document action buttons.
 *
 * Exactly one primary at a time, and it is whichever action comes next: claim an
 * unclaimed document, then confirm the one you hold. Before a transition is open neither
 * is next — "Start annotating" is, and it is the primary in its alert.
 *
 * Confirming is gated on the transition too: it tags every section reviewed-human, which
 * is precisely the draft-machine → reviewed-human change a version brackets. Gating only
 * the editor left this as an ungated second route to the same commitment.
 */
export const documentActionState = (
  selected: AnnotationSelection,
  draft: AnnotationDraft | null,
  isLoading: boolean,
): DocumentActionState => ({
  claimVariant: draft ? 'primary' : 'normal',
  confirmVariant: draft && (selected.claimedByMe || selected.reviewed || !selected.reviewObjectKey) ? 'primary' : 'normal',
  confirmDisabled: isLoading || !selected.reviewObjectKey || !draft,
});

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
