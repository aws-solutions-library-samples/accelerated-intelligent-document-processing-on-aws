// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's model: everything the page shows and does for one document, in one
// shape for every mode (a direct edit, a Production queue item, a Test Studio item).
// Each mode's model hook builds it from its I/O adapter; EditPage renders it and holds
// no mode-specific code. Types only.
import type React from 'react';
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import type { ConfigClassOption } from '../common/config-class-options';
import type { GroupedSection } from '../common/section-grouping';
import type { EditAction, EditActionsProps } from './EditScreen';
import type { EditorNotice } from './EditorNotices';
import type { SectionEditorContext } from './SectionEditor';
import type { LoadedSection, ReviewPage, ReviewSectionRef } from './types';
import type { SectionEvaluation } from './use-section-evaluation';

export type ReviewActivity = ReviewItemDetail['history'];
export type EditorSlot = (ctx: SectionEditorContext) => React.ReactNode;

/** Where the page goes once an action has finished the work. `to` is already resolved for the caller (useLeavePath applied). */
export interface EditExit {
  to: string;
  message: string;
  /** WorkItem.id dropped from Home › Tasks at once. */
  workItemId?: string;
}

export interface EditNavigation {
  /** The breadcrumbs, by where the page was opened from (see review-queues/breadcrumbs.ts). */
  trail: BreadcrumbGroupProps.Item[];
  /** Side-navigation entry to highlight. null = none; undefined = derived from the URL. */
  activeHref?: string | null;
}

export interface EditDocumentHeader {
  /** documentName(objectKey). */
  title: string;
  /** In parentheses after the title. */
  status: string | null;
  objectKey: string;
  /** What View shows in the editor's place; null = no Edit | View switch. */
  source: { objectKey: string; bucket?: string } | null;
  /** Summary in Reports; omitted = no Summary item. */
  summary?: { reportUri?: string; unavailableReason?: string };
  /**
   * Policy Rule Validation Summary in Reports; omitted = no such item. `reportUri` is the
   * document's policy rule validation report; without one the item stays in the menu,
   * disabled with `unavailableReason`.
   */
  ruleValidation?: { reportUri?: string; unavailableReason?: string };
  /** Offer Processing Report. */
  offerReport: boolean;
  /** After the status: a badge with hover text (Test Studio: the open version's `vN → vM`). */
  badge?: { text: string; hover: string };
}

/** The bar's buttons and menu items (EditActionsProps' show/disabled fields), owned by the model. */
export type EditBar = Pick<
  EditActionsProps,
  | 'showSave'
  | 'saveDisabledReason'
  | 'showCancel'
  | 'cancelDisabledReason'
  | 'showSkip'
  | 'skipDisabledReason'
  | 'showReject'
  | 'rejectDisabledReason'
  | 'showClaim'
  | 'showRegroup'
  | 'regroupDisabledReason'
  | 'showBaseline'
  | 'baselineDisabledReason'
  | 'primaryDisabledReason'
  | 'primaryHint'
  | 'showRefresh'
  | 'refreshDisabledReason'
> & { offerHistory: boolean };

export interface EditEditor {
  sections: ReviewSectionRef[];
  pages: ReviewPage[];
  pagesLoading?: boolean;
  /** Fields are read-only. True whenever `readOnlyReason` is set. */
  readOnly: boolean;
  /**
   * Why the fields are read-only, shown as the page's first notice. With `claim` (and the
   * bar's Claim shown) the notice offers Claim; `readOnlyAction` is any other unlock
   * action the mode offers (Production: Take over).
   */
  readOnlyReason?: string;
  readOnlyAction?: React.ReactNode;
  loadSection: (sectionId: string) => Promise<LoadedSection>;
  saveSection: (sectionId: string, data: Record<string, unknown>) => Promise<void | Record<string, unknown>>;
  onSaved?: (sectionId: string) => void;
  onLoaded?: (sectionId: string, data: Record<string, unknown>) => void;
  reloadToken?: number;
  /** `?section=`. */
  initialSectionId?: string | null;
  /** `?field=`. */
  focusFieldPath?: string | null;
  buildFieldLink?: ((fieldPath: string) => string) | null;
  autosaveMs?: number;
  confirmSectionSwitch?: boolean;
  recordEditHistory?: { editedBy: string; source: string };
  mergedConfig?: Record<string, unknown>;
  evaluation?: SectionEvaluation;
  defaultFilter?: 'none' | 'confidence-alerts';
  unsavedChangesMessage?: string;
  /** Mode-specific editor content (Test Studio: the pages-moved warning, the re-extract notice and pages field, the no-fields text, the page-image notices). */
  slots?: { headerExtras?: EditorSlot; classSlot?: EditorSlot; emptyFieldsSlot?: EditorSlot; pagesNotice?: React.ReactNode };
  /** Shown when there are no sections. */
  emptyText: string;
}

export interface EditBoard {
  pages: { kind: 'thumbnails'; pages: ReviewPage[] } | { kind: 'rendered'; pages: { id: number; imageUri: string }[] };
  /** Every section of the document in board terms; the open section's unsaved class is taken from openData. */
  sections: (openSectionId: string | null, openData: Record<string, unknown> | null) => GroupedSection[];
  classOptions?: ConfigClassOption[];
  canChangeClass: boolean;
  consequence: React.ReactNode;
  /** Before opening (an Admin claims an unassigned Production item); false keeps it closed. */
  beforeOpen?: () => Promise<boolean>;
  /** Persist. Resolves the success toast (or null); throws with the message shown inside the board. */
  save: (next: GroupedSection[]) => Promise<string | null>;
  /**
   * Offer Save and Re-extract (the document page): why it can't run for the sections a
   * draft changes, and the re-extract of those sections once the grouping is saved, which
   * resolves the success toast and throws with the reason it failed.
   */
  reextract?: {
    disabledReason: (changed: GroupedSection[]) => string | undefined;
    run: (changed: GroupedSection[]) => Promise<string>;
  };
}

export interface EditSkip {
  header: string;
  reasons: string[];
  body: React.ReactNode;
  run: (reason: string, note?: string) => Promise<EditExit>;
}

/** Reject: end the review with the document rejected, for a reason from the queue's list. */
export interface EditReject {
  header: string;
  reasons: string[];
  body: React.ReactNode;
  /** The label of the button that confirms the reason. */
  confirmLabel: string;
  run: (reason: string, note?: string) => Promise<EditExit>;
}

/** Send back: discard the review and process the document again. The server decides where the run starts. */
export interface EditSendBack {
  run: (note?: string) => Promise<EditExit>;
}

/**
 * What Process Changes asks first, after saving the open section's edits: the reprocessing
 * it triggers (direct edit; the dialog shows the steps the server says the run will do,
 * which it reads for the document on the page), the new test-set version it opens (Test
 * Studio, no draft version open yet), or how the review ends (a Production item: complete
 * it, or send the document back). The review's dialog shows what each ending runs, which
 * it reads for the item: the document's `objectKey` and the `cycleId` of the review it is in.
 */
export type EditProcessConfirm =
  | { kind: 'reprocess' }
  | { kind: 'version' }
  | { kind: 'review-completion'; objectKey: string; cycleId: string };

export interface EditProcess {
  /** Ask first; omitted = run at once. */
  confirm?: EditProcessConfirm;
  run: () => Promise<EditExit | null>;
}

export interface EditFailures {
  surface: 'notice' | 'flash';
  /** Text for a failed action; null = reported elsewhere (Production's "assigned just now" notice). */
  describe: (action: EditAction, err: unknown) => string | null;
  /** After a failed action, e.g. re-read the item. */
  after?: (action: EditAction) => void;
}

export interface EditReady {
  kind: 'ready';
  header: EditDocumentHeader;
  notices: readonly (EditorNotice | false | null | undefined)[];
  /** Production: the item's Review Reason. */
  context?: React.ReactNode;
  bar: EditBar;
  /** null: nothing to edit here (Test Studio without TestSetBucket). */
  editor: EditEditor | null;
  board: EditBoard | null;
  history: { reviewActivity: ReviewActivity; baseline?: Record<string, unknown> | null };
  skip?: EditSkip;
  /** Production: reject the document. Offered with the bar's Reject. */
  reject?: EditReject;
  /** Production: send the document back, from the review-completion dialog. */
  sendBack?: EditSendBack;
  process: EditProcess;
  /** Resolves the success toast. */
  claim?: () => Promise<string>;
  /** Use As Evaluation Baseline; resolves the toast. */
  baseline?: () => Promise<string>;
  /** Toast after Save. */
  savedMessage?: string;
  /** Refresh: re-read the document (and item); throws when the read fails. */
  refresh?: () => Promise<void>;
  cancelTo: string;
  failures: EditFailures;
}

export interface EditLoading {
  kind: 'loading';
  text: string;
}

export interface EditFailure {
  kind: 'failure';
  type: 'warning' | 'error';
  header: string;
  body: React.ReactNode;
  retry?: () => void;
}

export interface EditModel {
  navigation: EditNavigation;
  state: EditLoading | EditFailure | EditReady;
}
