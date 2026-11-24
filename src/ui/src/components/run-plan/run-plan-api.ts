// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// What the server says about a run before it starts: the steps documents can be
// reprocessed from, the steps the run that ends a review will do, and the steps
// Process Changes will run.
import { generateClient } from '../../api/client-shim';
import {
  getProcessChangesPlan as getProcessChangesPlanQuery,
  getReprocessOptions as getReprocessOptionsQuery,
  getReviewCompletionPlan as getReviewCompletionPlanQuery,
} from '../../graphql/generated';
import type {
  GetProcessChangesPlanQueryVariables,
  GetReprocessOptionsQueryVariables,
  GetReviewCompletionPlanQueryVariables,
  PlannedRun,
  ReprocessOption,
  ReviewCompletionPlan,
} from '../../graphql/generated/operation-types';

const client = generateClient();

const required = <T>(value: T | null | undefined, field: string): T => {
  if (value == null) throw new Error(`${field} returned no result`);
  return value;
};

/**
 * For each document, its processing mode and the steps a reprocess can start at.
 * `version` and `revision` are the profile the reprocess would name: a later start is
 * refused under some of them, and the answer says which.
 */
export const getReprocessOptions = async (objectKeys: string[], version?: string, revision?: number): Promise<ReprocessOption[]> => {
  if (objectKeys.length === 0) return [];
  const variables: GetReprocessOptionsQueryVariables = { objectKeys };
  if (version) variables.version = version;
  if (revision !== undefined) variables.revision = revision;
  const r = await client.graphql({ query: getReprocessOptionsQuery, variables });
  return r.data?.getReprocessOptions ?? [];
};

/**
 * What finishing a review runs, for both endings: `complete` when the review is
 * completed and `sendBack` when the document is sent back. Each is the step the run
 * starts at, every step of the document's processing mode with its state, and the
 * sections the run extracts again. The server decides all of it. For the holder of the
 * item, and an Admin on an item that is open or assigned.
 */
export const getReviewCompletionPlan = async (objectKey: string, cycleId: string): Promise<ReviewCompletionPlan> => {
  const variables: GetReviewCompletionPlanQueryVariables = { objectKey, cycleId };
  const r = await client.graphql({ query: getReviewCompletionPlanQuery, variables });
  return required(r.data?.getReviewCompletionPlan, 'getReviewCompletionPlan');
};

/**
 * What Process Changes runs for a document outside a review: the step the run starts
 * at, every step of the document's processing mode with its state, and the sections the
 * run extracts again. The server decides all of it. It refuses (an error, not a plan)
 * a document in a review queue, one a run is working on, one the malware scan holds,
 * and a caller who may not process it.
 */
export const getProcessChangesPlan = async (objectKey: string): Promise<PlannedRun> => {
  const variables: GetProcessChangesPlanQueryVariables = { objectKey };
  const r = await client.graphql({ query: getProcessChangesPlanQuery, variables });
  return required(r.data?.getProcessChangesPlan, 'getProcessChangesPlan');
};
