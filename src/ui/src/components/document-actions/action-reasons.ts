// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Why an action on the document page is unavailable now: the reasons its bar, menus,
// dialogs and table rows show. Reasons shared with the edit page live in
// edit-screen/document-edit-status.ts and common/review-status.ts.

export const HISTORICAL_REASON = 'Not available while you view a past version. Choose Return to current version in Version History.';
export const LEGACY_REVIEW_REGROUP_REASON = "Available once this document's review is finished";
export const BDA_REPROCESS_REASON = 'Not available in BDA mode: reprocessing would leave the changed sections without extracted data.';
export const CONFIG_LOADING_REASON = "Loading this document's configuration…";
export const NO_PAGES_REASON = 'This document has no pages';
export const NO_PAGE_TEXT_REASON = 'This document has no page text';
export const REVIEWER_UNCLAIMED_REASON = 'Choose Start Review first: a Reviewer changes a document while holding its review.';
export const REVIEWER_FINISHED_REASON = "This document's review is finished, so a Reviewer can no longer change it.";
export const NO_PENDING_REVIEW_REASON = 'This document has no pending review';
export const NOT_ABORTABLE_REASON = 'Only a document that is still processing can be aborted';
export const NO_EVALUATION_REPORT_REASON = 'This document has no evaluation report';
export const NO_RULE_VALIDATION_REASON = 'This document has no rule validation summary';
export const RULE_VALIDATION_HISTORICAL_REASON = "Rule validation isn't kept for past versions";
export const NO_EXECUTION_REASON = 'This document has no processing run to show';
export const REEXTRACT_ORDER_ONLY_REASON = 'Only the page order changed, so nothing needs re-extracting. Choose Save page grouping.';
export const reextractNoClassReason = (ids: string[]): string =>
  `Choose a class for section${ids.length > 1 ? 's' : ''} ${ids.join(', ')} before re-extracting`;
export const CHOOSE_SECTIONS_REASON = 'Choose at least one section';
export const CHOOSE_PAGES_REASON = 'Choose at least one page';
export const SECTION_NO_CLASS_REASON = 'This section has no class';
export const SECTION_SKIPPED_REASON = 'This section is skipped by its configuration';
/** Use As Evaluation Baseline while its request is on its way (baselineCopyBlockedReason's wording). */
export const BASELINE_COPYING_REASON = 'A copy to the evaluation baseline is in progress';
/** A section row's Edit Data / View Data before the section has a result. */
export const SECTION_NO_DATA_REASON = 'This section has no data yet';
/** A page row's View / Edit Page Text when the page has no text. */
export const PAGE_NO_TEXT_REASON = 'No text available';
