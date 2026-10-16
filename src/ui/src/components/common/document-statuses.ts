// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Document (tracking row ObjectStatus) status sets shared by the list, the panel
// and the abort modal.

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

/** Statuses after which a document no longer changes on its own. */
export const TERMINAL_STATUSES: readonly string[] = ['COMPLETED', 'FAILED', 'ABORTED', 'REJECTED', 'REDACTED_SUPERSEDED'];

/** Statuses that wait on people rather than on processing, so they poll slowly. */
export const SLOW_POLL_STATUSES: readonly string[] = ['HITL_IN_PROGRESS'];
