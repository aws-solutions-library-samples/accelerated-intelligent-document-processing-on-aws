// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Why an action on the document page is unavailable now: the reasons its bar, menus,
// dialogs and table rows show. Reasons shared with the edit page live in
// edit-screen/document-edit-status.ts and common/review-status.ts.
//
// Each reason is a message, worded where it is shown. The exported constants are its English
// source text, for the screens that are not translated yet and for the tests that compare with it.
import { createIntl, defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

export const actionReasonMessages = defineMessages<{
  historical: NoMessageValues;
  legacyReviewRegroup: NoMessageValues;
  bdaReprocess: NoMessageValues;
  reviewOwner: NoMessageValues;
  configLoading: NoMessageValues;
  noPages: NoMessageValues;
  noPageText: NoMessageValues;
  reviewerUnclaimed: NoMessageValues;
  reviewerFinished: NoMessageValues;
  noPendingReview: NoMessageValues;
  notAbortable: NoMessageValues;
  noEvaluationReport: NoMessageValues;
  noRuleValidation: NoMessageValues;
  ruleValidationHistorical: NoMessageValues;
  noCost: NoMessageValues;
  noExecution: NoMessageValues;
  reextractOrderOnly: NoMessageValues;
  chooseSections: NoMessageValues;
  choosePages: NoMessageValues;
  sectionNoClass: NoMessageValues;
  sectionSkipped: NoMessageValues;
  baselineCopying: NoMessageValues;
  sectionNoData: NoMessageValues;
  pageNoText: NoMessageValues;
  reviewQueueOff: NoMessageValues;
  notFinished: NoMessageValues;
  alreadyInReview: NoMessageValues;
  noSkippableSelection: NoMessageValues;
  noSendableSelection: NoMessageValues;
  testStudioReviewMenu: NoMessageValues;
}>({
  historical: {
    id: 'documentActions.reasons.historical',
    defaultMessage: 'Not available while you view a past version. Choose Return to current version in Version History.',
    description:
      'Why an action of the document page is unavailable while the page shows a past version of the document. "Return to current version" is the label of a button in the Version History panel.',
  },
  legacyReviewRegroup: {
    id: 'documentActions.reasons.legacyReviewRegroup',
    defaultMessage: "Available once this document's review is finished",
    description:
      "Why Edit Page Grouping on the document page is unavailable while the document's review (outside the review queues) is in progress.",
  },
  bdaReprocess: {
    id: 'documentActions.reasons.bdaReprocess',
    defaultMessage: 'Not available in BDA mode: reprocessing would leave the changed sections without extracted data.',
    description:
      'Why an action of the document page that reprocesses the document is unavailable for a document processed with Bedrock Data Automation (BDA).',
  },
  reviewOwner: {
    id: 'documentActions.reasons.reviewOwner',
    defaultMessage: "Needs an Admin, or the reviewer who holds this document's open review.",
    description:
      "Why an action of the document page that reprocesses the document is unavailable to a user who is not an Admin and does not hold the document's open review. Admin is a user role.",
  },
  configLoading: {
    id: 'documentActions.reasons.configLoading',
    defaultMessage: "Loading this document's configuration…",
    description:
      "Why an action of the document page that reprocesses the document is unavailable while the document's configuration loads.",
  },
  noPages: {
    id: 'documentActions.reasons.noPages',
    defaultMessage: 'This document has no pages',
    description: 'Why Reset Page Class on the document page is unavailable: the document has no pages.',
  },
  noPageText: {
    id: 'documentActions.reasons.noPageText',
    defaultMessage: 'This document has no page text',
    description: 'Why Edit Page Text on the document page is unavailable: no page of the document has text.',
  },
  reviewerUnclaimed: {
    id: 'documentActions.reasons.reviewerUnclaimed',
    defaultMessage: 'Choose Start Review first: a Reviewer changes a document while holding its review.',
    description:
      'Why an action of the document page that reprocesses the document is unavailable to a Reviewer while nobody holds its review. "Start Review" is the label of a button of the page; Reviewer is a user role.',
  },
  reviewerFinished: {
    id: 'documentActions.reasons.reviewerFinished',
    defaultMessage: "This document's review is finished, so a Reviewer can no longer change it.",
    description:
      "Why an action of the document page that reprocesses the document is unavailable to a Reviewer once the document's review is finished. Reviewer is a user role.",
  },
  noPendingReview: {
    id: 'documentActions.reasons.noPendingReview',
    defaultMessage: 'This document has no pending review',
    description: 'Why Skip All Reviews on the document page is unavailable: no review of the document is waiting.',
  },
  notAbortable: {
    id: 'documentActions.reasons.notAbortable',
    defaultMessage: 'Only a document that is still processing can be aborted',
    description: 'Why Abort on the document page is unavailable: the document is no longer processing.',
  },
  noEvaluationReport: {
    id: 'documentActions.reasons.noEvaluationReport',
    defaultMessage: 'This document has no evaluation report',
    description: "Why Evaluation Report in the document page's Reports menu is unavailable.",
  },
  noRuleValidation: {
    id: 'documentActions.reasons.noRuleValidation',
    defaultMessage: 'This document has no rule validation summary',
    description: "Why Rule Validation Summary in the document page's Reports menu is unavailable.",
  },
  ruleValidationHistorical: {
    id: 'documentActions.reasons.ruleValidationHistorical',
    defaultMessage: "Rule validation isn't kept for past versions",
    description: "Why Rule Validation Summary in the document page's Reports menu is unavailable while the page shows a past version.",
  },
  noCost: {
    id: 'documentActions.reasons.noCost',
    defaultMessage: 'This document has no cost data yet',
    description: "Why Estimated Cost in the document page's Reports menu is unavailable: the document has no usage recorded yet.",
  },
  noExecution: {
    id: 'documentActions.reasons.noExecution',
    defaultMessage: 'This document has no processing run to show',
    description: "Why Processing Flow in the document page's Reports menu is unavailable: the document has no workflow run.",
  },
  reextractOrderOnly: {
    id: 'documentActions.reasons.reextractOrderOnly',
    defaultMessage: 'Only the page order changed, so nothing needs re-extracting. Choose Save page grouping.',
    description:
      'Why Save and Re-extract on the Edit Page Grouping board of the document page is unavailable when only the order of pages changed. "Save page grouping" is the label of the board\'s other save button.',
  },
  chooseSections: {
    id: 'documentActions.reasons.chooseSections',
    defaultMessage: 'Choose at least one section',
    description: 'Why the Re-extract button of the Re-extract Sections dialog is unavailable: no section is chosen.',
  },
  choosePages: {
    id: 'documentActions.reasons.choosePages',
    defaultMessage: 'Choose at least one page',
    description: 'Why the Reset Page Class button of its dialog is unavailable: no page is chosen.',
  },
  sectionNoClass: {
    id: 'documentActions.reasons.sectionNoClass',
    defaultMessage: 'This section has no class',
    description: "Why a section can't be chosen in the Re-extract Sections dialog: it has no class.",
  },
  sectionSkipped: {
    id: 'documentActions.reasons.sectionSkipped',
    defaultMessage: 'This section is skipped by its configuration',
    description: "Why a section can't be chosen in the Re-extract Sections dialog: the document's configuration skips its class.",
  },
  baselineCopying: {
    id: 'documentActions.reasons.baselineCopying',
    defaultMessage: 'A copy to the evaluation baseline is in progress',
    description:
      'Why Use As Evaluation Baseline on the document page is unavailable while its request to copy the document to the evaluation baseline is on its way.',
  },
  sectionNoData: {
    id: 'documentActions.reasons.sectionNoData',
    defaultMessage: 'This section has no data yet',
    description: "Why a section row's Edit Data or View Data in the Document Sections table is unavailable: the section has no result yet.",
  },
  pageNoText: {
    id: 'documentActions.reasons.pageNoText',
    defaultMessage: 'No text available',
    description: "Why a page row's View Page Text or Edit Page Text in the Document Pages table is unavailable: the page has no text.",
  },
  reviewQueueOff: {
    id: 'documentActions.reasons.reviewQueueOff',
    defaultMessage: 'The Default review queue is off',
    description: 'Why Send to review is unavailable: the review queue named Default is turned off. Default is the name of a queue.',
  },
  notFinished: {
    id: 'documentActions.reasons.notFinished',
    defaultMessage: 'This document has not finished processing',
    description: 'Why Send to review on the document page is unavailable: the document is still processing.',
  },
  alreadyInReview: {
    id: 'documentActions.reasons.alreadyInReview',
    defaultMessage: 'This document is already in review',
    description: 'Why Send to review on the document page is unavailable: the document is already in a review.',
  },
  noSkippableSelection: {
    id: 'documentActions.reasons.noSkippableSelection',
    defaultMessage: 'Select documents with a pending review',
    description:
      "Why Skip All Reviews for a selection (the document list's Review menu, a review queue's More actions menu) is unavailable: no selected document has a pending review.",
  },
  noSendableSelection: {
    id: 'documentActions.reasons.noSendableSelection',
    defaultMessage: 'Select finished documents that are not in review',
    description:
      "Why Send to review in the document list's Review menu is unavailable: no selected document has finished processing and is outside review.",
  },
  testStudioReviewMenu: {
    id: 'documentActions.reasons.testStudioReviewMenu',
    defaultMessage: 'Review actions apply to production documents',
    description: "Why the document list's Review menu is unavailable in its Test Studio view. Test Studio is the name of a feature.",
  },
});

const messages = defineMessages<{ reextractNoClass: { readonly count: number; readonly ids: string } }>({
  reextractNoClass: {
    id: 'documentActions.reasons.reextractNoClass',
    defaultMessage: 'Choose a class for {count, plural, one {section} other {sections}} {ids} before re-extracting',
    description:
      'Why Save and Re-extract on the Edit Page Grouping board of the document page is unavailable: changed sections have no class. {count} is how many; {ids} lists their numbers, separated by commas.',
  },
});

/** Formats the English source text for callers that pass no `intl`. */
const englishIntl = createIntl({ locale: 'en', defaultLocale: 'en' });

export const HISTORICAL_REASON = String(actionReasonMessages.historical.defaultMessage);
export const LEGACY_REVIEW_REGROUP_REASON = String(actionReasonMessages.legacyReviewRegroup.defaultMessage);
export const BDA_REPROCESS_REASON = String(actionReasonMessages.bdaReprocess.defaultMessage);
/** processChanges for a caller who is not an Admin: the server's open-review owner rule. */
export const REVIEW_OWNER_REASON = String(actionReasonMessages.reviewOwner.defaultMessage);
export const CONFIG_LOADING_REASON = String(actionReasonMessages.configLoading.defaultMessage);
export const NO_PAGES_REASON = String(actionReasonMessages.noPages.defaultMessage);
export const NO_PAGE_TEXT_REASON = String(actionReasonMessages.noPageText.defaultMessage);
export const REVIEWER_UNCLAIMED_REASON = String(actionReasonMessages.reviewerUnclaimed.defaultMessage);
export const REVIEWER_FINISHED_REASON = String(actionReasonMessages.reviewerFinished.defaultMessage);
export const NO_PENDING_REVIEW_REASON = String(actionReasonMessages.noPendingReview.defaultMessage);
export const NOT_ABORTABLE_REASON = String(actionReasonMessages.notAbortable.defaultMessage);
export const NO_EVALUATION_REPORT_REASON = String(actionReasonMessages.noEvaluationReport.defaultMessage);
export const NO_RULE_VALIDATION_REASON = String(actionReasonMessages.noRuleValidation.defaultMessage);
export const RULE_VALIDATION_HISTORICAL_REASON = String(actionReasonMessages.ruleValidationHistorical.defaultMessage);
export const NO_COST_REASON = String(actionReasonMessages.noCost.defaultMessage);
export const NO_EXECUTION_REASON = String(actionReasonMessages.noExecution.defaultMessage);
export const REEXTRACT_ORDER_ONLY_REASON = String(actionReasonMessages.reextractOrderOnly.defaultMessage);
/**
 * Why Save and Re-extract can't run while changed sections have no class: in the UI's language
 * with `intl`, the English source text without.
 */
export const reextractNoClassReason = (ids: string[], intl?: IntlShape): string =>
  (intl ?? englishIntl).formatMessage(messages.reextractNoClass, { count: ids.length, ids: ids.join(', ') });
export const CHOOSE_SECTIONS_REASON = String(actionReasonMessages.chooseSections.defaultMessage);
export const CHOOSE_PAGES_REASON = String(actionReasonMessages.choosePages.defaultMessage);
export const SECTION_NO_CLASS_REASON = String(actionReasonMessages.sectionNoClass.defaultMessage);
export const SECTION_SKIPPED_REASON = String(actionReasonMessages.sectionSkipped.defaultMessage);
/** Use As Evaluation Baseline while its request is on its way (baselineCopyBlockedReason's wording). */
export const BASELINE_COPYING_REASON = String(actionReasonMessages.baselineCopying.defaultMessage);
/** A section row's Edit Data / View Data before the section has a result. */
export const SECTION_NO_DATA_REASON = String(actionReasonMessages.sectionNoData.defaultMessage);
/** A page row's View / Edit Page Text when the page has no text. */
export const PAGE_NO_TEXT_REASON = String(actionReasonMessages.pageNoText.defaultMessage);
/** Send to review needs the Default review queue (Send to review, bulk and single). */
export const REVIEW_QUEUE_OFF_REASON = String(actionReasonMessages.reviewQueueOff.defaultMessage);
/** Send to review of a document still processing. */
export const NOT_FINISHED_REASON = String(actionReasonMessages.notFinished.defaultMessage);
/** Send to review of a document already in a review. */
export const ALREADY_IN_REVIEW_REASON = String(actionReasonMessages.alreadyInReview.defaultMessage);
/** Skip All Reviews (N selected) with no selected document that has a pending review. */
export const NO_SKIPPABLE_SELECTION_REASON = String(actionReasonMessages.noSkippableSelection.defaultMessage);
/** Send to review (N selected) with no selected finished document outside review. */
export const NO_SENDABLE_SELECTION_REASON = String(actionReasonMessages.noSendableSelection.defaultMessage);
/** The Document List's Review menu in the Test Studio view. */
export const TEST_STUDIO_REVIEW_MENU_REASON = String(actionReasonMessages.testStudioReviewMenu.defaultMessage);
