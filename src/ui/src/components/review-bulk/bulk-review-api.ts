// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Typed calls to bulk Skip All Reviews, its jobs, and Send to review.
import { generateClient } from '../../api/client-shim';
import {
  bulkSkipAllReviews as bulkSkipAllReviewsMutation,
  cancelBulkReviewJob as cancelBulkReviewJobMutation,
  deleteBulkReviewJob as deleteBulkReviewJobMutation,
  getBulkReviewJob as getBulkReviewJobQuery,
  listBulkReviewJobs as listBulkReviewJobsQuery,
  sendToReview as sendToReviewMutation,
} from '../../graphql/generated';
import type {
  BulkReviewJob,
  BulkReviewJobPage,
  BulkReviewJobResults,
  BulkSkipAllReviewsInput,
  BulkSkipAllReviewsResult,
  SendToReviewResult,
} from '../../graphql/generated/schema-types';

const client = generateClient();

const required = <T>(value: T | null | undefined, field: string): T => {
  if (value == null) throw new Error(`${field} returned no result`);
  return value;
};

export type BulkSkipScope =
  | { mode: 'SELECTED'; objectKeys: string[] }
  | { mode: 'PROFILE'; profile: string; submittedFrom: string; submittedTo: string };

export interface BulkSkipChoice {
  reason: string;
  note?: string;
  resubmit: boolean;
}

const input = (scope: BulkSkipScope, extra: Partial<BulkSkipAllReviewsInput>): BulkSkipAllReviewsInput => ({
  ...scope,
  reason: extra.reason ?? 'Other (see note)',
  ...extra,
});

/** What a bulk skip would do; writes nothing. The reason is a placeholder the server validates. */
export const dryRunBulkSkip = async (scope: BulkSkipScope): Promise<BulkSkipAllReviewsResult> => {
  const r = await client.graphql({
    query: bulkSkipAllReviewsMutation,
    variables: { input: input(scope, { dryRun: true }) },
  });
  return required(r.data?.bulkSkipAllReviews, 'bulkSkipAllReviews');
};

/** Run a bulk skip (inline, or as a job above the sync limit). */
export const runBulkSkip = async (scope: BulkSkipScope, choice: BulkSkipChoice): Promise<BulkSkipAllReviewsResult> => {
  const r = await client.graphql({
    query: bulkSkipAllReviewsMutation,
    variables: {
      input: input(scope, {
        reason: choice.reason,
        note: choice.note,
        resubmit: choice.resubmit,
        clientRequestId: crypto.randomUUID(),
      }),
    },
  });
  return required(r.data?.bulkSkipAllReviews, 'bulkSkipAllReviews');
};

export const getBulkJob = async (variables: {
  jobId: string;
  outcome?: string | null;
  limit?: number;
  nextToken?: string | null;
}): Promise<BulkReviewJobResults> => {
  const r = await client.graphql({ query: getBulkReviewJobQuery, variables });
  return required(r.data?.getBulkReviewJob, 'getBulkReviewJob');
};

export const listBulkJobs = async (variables: { limit?: number; nextToken?: string | null } = {}): Promise<BulkReviewJobPage> => {
  const r = await client.graphql({ query: listBulkReviewJobsQuery, variables });
  return required(r.data?.listBulkReviewJobs, 'listBulkReviewJobs');
};

/** Stop a queued or running job before its next page; returns the job, now Cancelled. */
export const cancelBulkJob = async (jobId: string): Promise<BulkReviewJob> => {
  const r = await client.graphql({ query: cancelBulkReviewJobMutation, variables: { jobId } });
  return required(r.data?.cancelBulkReviewJob, 'cancelBulkReviewJob');
};

/** Delete a finished job (Succeeded, Failed or Cancelled) from Home › Jobs. */
export const deleteBulkJob = async (jobId: string): Promise<void> => {
  const r = await client.graphql({ query: deleteBulkReviewJobMutation, variables: { jobId } });
  required(r.data?.deleteBulkReviewJob, 'deleteBulkReviewJob');
};

export const sendToReview = async (objectKeys: string[], reason: string, note?: string): Promise<SendToReviewResult> => {
  const r = await client.graphql({ query: sendToReviewMutation, variables: { objectKeys, reason, note } });
  return required(r.data?.sendToReview, 'sendToReview');
};
