// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Document (tracking row ObjectStatus) status sets shared by the list, the panel
// and the abort modal.

import { MALWARE_REFUSED_STATUSES } from './malware-scan';

/** Statuses the abort resolver accepts (abort_workflow_resolver ABORTABLE_STATUSES). */
export const ABORTABLE_STATUSES: readonly string[] = [
  'QUEUED',
  'RUNNING',
  'PREPROCESSING',
  'OCR',
  'CLASSIFYING',
  'EXTRACTING',
  'ASSESSING',
  'POSTPROCESSING',
  'HITL_IN_PROGRESS',
  'SUMMARIZING',
  'EVALUATING',
];

/**
 * Statuses after which a document no longer changes on its own. A status missing
 * here is re-fetched for as long as the page stays open. MALWARE_SCANNING is
 * deliberately NOT here: the verdict is still coming, so the poll is what makes
 * the status change visible without a manual refresh.
 */
export const TERMINAL_STATUSES: readonly string[] = [
  'COMPLETED',
  'FAILED',
  'ABORTED',
  'REJECTED',
  'REDACTED_SUPERSEDED',
  ...MALWARE_REFUSED_STATUSES,
];

/** Statuses that wait on people rather than on processing, so they poll slowly. */
export const SLOW_POLL_STATUSES: readonly string[] = ['HITL_IN_PROGRESS'];
