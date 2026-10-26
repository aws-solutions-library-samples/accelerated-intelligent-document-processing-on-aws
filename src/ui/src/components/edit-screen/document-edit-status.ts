// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// When a document's results may be edited directly: shared by the unified editor's
// direct edit and the document page's Edit button.

/** Statuses in which a document's results may be edited (processing has finished). */
export const EDITABLE_STATUSES: readonly string[] = ['COMPLETED', 'FAILED', 'ABORTED', 'REDACTED_SUPERSEDED', 'HITL_IN_PROGRESS'];

/** Why the direct edit is unavailable while a document is processing. */
export const PROCESSING_REASON = 'Available when processing finishes';

export const isEditableStatus = (status: string | null | undefined): boolean => EDITABLE_STATUSES.includes(String(status ?? ''));
