// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The prefix the capacity calculator puts on a recommendation describing a
 * Bedrock quota it could not read. It must equal `QUOTA_NOT_READ_PREFIX` in
 * `src/lambda/calculate_capacity/index.py`; a test there asserts the two match.
 */
export const QUOTA_NOT_READ_PREFIX = '⚠️ Quota not read:';

/**
 * The recommendations that report an unreadable quota, in the order the
 * calculator returned them. Each names the model and quota code whose steps the
 * quota table leaves out.
 */
export const quotaNotReadWarnings = (recommendations: ReadonlyArray<string | null | undefined> | null | undefined): string[] =>
  (recommendations ?? []).filter((r): r is string => typeof r === 'string' && r.startsWith(QUOTA_NOT_READ_PREFIX));
