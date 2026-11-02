// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Review Studio › Review Management: the system-wide queue definitions, the Annotators work is
// assigned to, and assigning Test Studio documents. Production items are assigned
// with reassignReviewItem and unassigned with forceReleaseReviewItem (review-api.ts).
// A queue is worked by the Annotators listed on it; Annotators claim its documents.
import { generateClient } from '../../api/client-shim';
import {
  assignTestSetDocuments as assignTestSetDocumentsMutation,
  claimTestSetDocument as claimTestSetDocumentMutation,
  createReviewQueue as createReviewQueueMutation,
  deleteReviewQueue as deleteReviewQueueMutation,
  listMyTestSetTasks as listMyTestSetTasksQuery,
  listAnnotators as listAnnotatorsQuery,
  listReviewQueues as listReviewQueuesQuery,
  listTestSetQueues as listTestSetQueuesQuery,
  unassignTestSetDocuments as unassignTestSetDocumentsMutation,
  updateTestSetQueue as updateTestSetQueueMutation,
  updateReviewQueue as updateReviewQueueMutation,
} from '../../graphql/generated';
import type {
  ReviewQueueDefinition,
  ReviewQueueDefinitionInput,
  TestSetDocumentAssignment,
  TestSetQueueSettings,
  TestSetQueueSettingsInput,
  TestSetQueueSummary,
  TestSetTask,
} from '../../graphql/generated/operation-types';

const client = generateClient();

/**
 * Emails of the Annotators: the members of the Cognito Annotator group, the only
 * people work can be assigned to (listAnnotators). A user is an Annotator when User
 * Management gives them the Annotator role or the external IdP maps them to it.
 */
export const listAnnotatorEmails = async (): Promise<string[]> => {
  const r = await client.graphql({ query: listAnnotatorsQuery });
  return (r.data?.listAnnotators ?? []).filter((e): e is string => !!e);
};

/** Annotators a queue's items may go to: the ones listed on it (an empty list means none). */
export const eligibleAnnotators = (
  annotators: readonly string[],
  queue?: Pick<ReviewQueueDefinition, 'eligibleAnnotators'> | null,
): string[] => {
  const eligible = new Set((queue?.eligibleAnnotators ?? []).map((e) => e.toLowerCase()));
  return annotators.filter((e) => eligible.has(e.toLowerCase()));
};

// ------------------------------------------------------------- queue definitions

export const listReviewQueues = async (): Promise<ReviewQueueDefinition[]> => {
  const r = await client.graphql({ query: listReviewQueuesQuery });
  return (r.data?.listReviewQueues ?? []).filter((q): q is ReviewQueueDefinition => !!q);
};

export const createReviewQueue = async (input: ReviewQueueDefinitionInput): Promise<ReviewQueueDefinition> => {
  const r = await client.graphql({ query: createReviewQueueMutation, variables: { input } });
  const q = r.data?.createReviewQueue;
  if (!q) throw new Error('createReviewQueue returned no result');
  return q;
};

export const updateReviewQueue = async (input: ReviewQueueDefinitionInput): Promise<ReviewQueueDefinition> => {
  const r = await client.graphql({ query: updateReviewQueueMutation, variables: { input } });
  const q = r.data?.updateReviewQueue;
  if (!q) throw new Error('updateReviewQueue returned no result');
  return q;
};

export const deleteReviewQueue = async (id: string): Promise<void> => {
  await client.graphql({ query: deleteReviewQueueMutation, variables: { id } });
};

// ------------------------------------------------------------ Test Studio queues

export const assignTestSetDocuments = async (
  testSetId: string,
  objectKeys: string[],
  assigneeEmail: string,
): Promise<TestSetDocumentAssignment[]> => {
  const r = await client.graphql({ query: assignTestSetDocumentsMutation, variables: { testSetId, objectKeys, assigneeEmail } });
  return (r.data?.assignTestSetDocuments ?? []).filter((a): a is TestSetDocumentAssignment => !!a);
};

export const unassignTestSetDocuments = async (testSetId: string, objectKeys: string[]): Promise<TestSetDocumentAssignment[]> => {
  const r = await client.graphql({ query: unassignTestSetDocumentsMutation, variables: { testSetId, objectKeys } });
  return (r.data?.unassignTestSetDocuments ?? []).filter((a): a is TestSetDocumentAssignment => !!a);
};

/**
 * Claim one unassigned, unreviewed Test Studio document: it is assigned to the caller
 * (no expiry) and shows in Home › Tasks. Only an Annotator listed on the set's queue may.
 */
export const claimTestSetDocument = async (testSetId: string, objectKey: string): Promise<TestSetDocumentAssignment> => {
  const r = await client.graphql({ query: claimTestSetDocumentMutation, variables: { testSetId, objectKey } });
  const a = r.data?.claimTestSetDocument;
  if (!a) throw new Error('claimTestSetDocument returned no result');
  return a;
};

/**
 * The Test Studio queues, with their stored counts (one DynamoDB-only call). An Admin
 * gets every test set; an Annotator only the sets whose queue lists them.
 */
export const listTestSetQueues = async (): Promise<TestSetQueueSummary[]> => {
  const r = await client.graphql({ query: listTestSetQueuesQuery });
  return (r.data?.listTestSetQueues ?? []).filter((q): q is TestSetQueueSummary => !!q);
};

/** The caller's assigned Test Studio documents, across every set. */
export const listMyTestSetTasks = async (): Promise<TestSetTask[]> => {
  const r = await client.graphql({ query: listMyTestSetTasksQuery });
  return (r.data?.listMyTestSetTasks ?? []).filter((t): t is TestSetTask => !!t);
};

/** A test set's queue settings: description, priority, time limit, eligible Annotators, checklist. */
export const updateTestSetQueue = async (input: TestSetQueueSettingsInput): Promise<TestSetQueueSettings> => {
  const r = await client.graphql({ query: updateTestSetQueueMutation, variables: { input } });
  const q = r.data?.updateTestSetQueue;
  if (!q) throw new Error('updateTestSetQueue returned no result');
  return q;
};
