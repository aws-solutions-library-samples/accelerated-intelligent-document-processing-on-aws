// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// When a document's results may be edited directly, and the reasons shared by the
// edit page and the document page's bar.
import { defineMessages, type NoMessageValues } from 'react-intl';

/** Statuses in which a document's results may be edited (processing has finished). */
export const EDITABLE_STATUSES: readonly string[] = ['COMPLETED', 'FAILED', 'ABORTED', 'REDACTED_SUPERSEDED', 'HITL_IN_PROGRESS'];

/**
 * The shared reasons, worded where they are shown. The exported constants below are their
 * English source text, for the screens that are not translated yet.
 */
export const documentEditStatusMessages = defineMessages<{
  processing: NoMessageValues;
  inReviewShort: NoMessageValues;
  noSummary: NoMessageValues;
  noSections: NoMessageValues;
  bdaRegroup: NoMessageValues;
}>({
  processing: {
    id: 'editScreen.documentStatus.processing',
    defaultMessage: 'Available when processing finishes',
    description: 'Why an action about a document, or its direct edit, is unavailable while the document is still processing.',
  },
  inReviewShort: {
    id: 'editScreen.documentStatus.inReviewShort',
    defaultMessage: 'This document is in a review queue',
    description:
      'Why a menu item about a document, such as Summary in the Reports menu, is unavailable while the document is in a review queue.',
  },
  noSummary: {
    id: 'editScreen.documentStatus.noSummary',
    defaultMessage: 'This document has no summary',
    description: 'Why Summary in the Reports menu is unavailable: the document has no summary report.',
  },
  noSections: {
    id: 'editScreen.documentStatus.noSections',
    defaultMessage: 'This document has no sections',
    description: 'Why Edit Page Grouping, or Re-extract Sections on the document page, is unavailable: the document has no sections.',
  },
  bdaRegroup: {
    id: 'editScreen.documentStatus.bdaRegroup',
    defaultMessage:
      'Page grouping cannot be changed for a document processed with Bedrock Data Automation (BDA). Field values can still be edited.',
    description:
      'Why Edit Page Grouping is unavailable for a document processed with Bedrock Data Automation (BDA), the name of an AWS service.',
  },
});

/** Why the direct edit is unavailable while a document is processing. */
export const PROCESSING_REASON = String(documentEditStatusMessages.processing.defaultMessage);

export const isEditableStatus = (status: string | null | undefined): boolean => EDITABLE_STATUSES.includes(String(status ?? ''));

/** Why an action about a finished document is unavailable while it is in a review queue (a menu item's short reason). */
export const IN_REVIEW_SHORT_REASON = String(documentEditStatusMessages.inReviewShort.defaultMessage);

/** Summary, for a document with no summary report. */
export const NO_SUMMARY_REASON = String(documentEditStatusMessages.noSummary.defaultMessage);

/** Edit Page Grouping, for a document with no sections. */
export const NO_SECTIONS_REASON = String(documentEditStatusMessages.noSections.defaultMessage);

/**
 * Why Edit Page Grouping is not offered in BDA mode. A BDA reprocess keeps the sections a
 * document already has, so a structural change is not carried through it.
 */
export const BDA_REGROUP_REASON = String(documentEditStatusMessages.bdaRegroup.defaultMessage);
