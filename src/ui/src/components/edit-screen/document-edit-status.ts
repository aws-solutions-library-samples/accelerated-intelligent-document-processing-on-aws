// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// When a document's results may be edited directly, and the reasons shared by the
// edit page and the document page's bar.

/** Statuses in which a document's results may be edited (processing has finished). */
export const EDITABLE_STATUSES: readonly string[] = ['COMPLETED', 'FAILED', 'ABORTED', 'REDACTED_SUPERSEDED', 'HITL_IN_PROGRESS'];

/** Why the direct edit is unavailable while a document is processing. */
export const PROCESSING_REASON = 'Available when processing finishes';

export const isEditableStatus = (status: string | null | undefined): boolean => EDITABLE_STATUSES.includes(String(status ?? ''));

/** Why an action about a finished document is unavailable while it is in a review queue (a menu item's short reason). */
export const IN_REVIEW_SHORT_REASON = 'This document is in a review queue';

/** Summary, for a document with no summary report. */
export const NO_SUMMARY_REASON = 'This document has no summary';

/** Edit Page Class, for a document with no sections. */
export const NO_SECTIONS_REASON = 'This document has no sections';
