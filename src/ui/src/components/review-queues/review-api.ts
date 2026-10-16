// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Typed calls to the review queue API, shared by the review queue pages.
import { generateClient } from '../../api/client-shim';
import { extractGraphQLErrorMessage } from '../../hooks/utils/graphql-error';
import {
  completeReviewItem as completeReviewItemMutation,
  forceReleaseReviewItem as forceReleaseReviewItemMutation,
  getMyReviewQueues as getMyReviewQueuesQuery,
  getReviewItem as getReviewItemQuery,
  getReviewQueuesOverview as getReviewQueuesOverviewQuery,
  listReviewQueueItems as listReviewQueueItemsQuery,
  moveReviewItem as moveReviewItemMutation,
  reassignReviewItem as reassignReviewItemMutation,
  rejectReviewItem as rejectReviewItemMutation,
  saveReviewDraft as saveReviewDraftMutation,
} from '../../graphql/generated';
import type {
  MyReviewQueues,
  ReviewDraft,
  ReviewItem,
  ReviewItemDetail,
  ReviewItemPage,
  ReviewQueuesOverview,
} from '../../graphql/generated/operation-types';

const client = generateClient();

export interface ItemRef {
  objectKey: string;
  cycleId: string;
}

const required = <T>(value: T | null | undefined, field: string): T => {
  if (value == null) throw new Error(`${field} returned no result`);
  return value;
};

const ref = (item: ItemRef): ItemRef => ({ objectKey: item.objectKey, cycleId: item.cycleId });

export const fetchMyReviewQueues = async (itemLimit = 100): Promise<MyReviewQueues> => {
  const r = await client.graphql({ query: getMyReviewQueuesQuery, variables: { itemLimit } });
  return required(r.data?.getMyReviewQueues, 'getMyReviewQueues');
};

export const fetchQueueItems = async (variables: {
  queueProfile?: string;
  queueName?: string;
  claimable?: boolean;
  limit?: number;
  nextToken?: string | null;
}): Promise<ReviewItemPage> => {
  const r = await client.graphql({ query: listReviewQueueItemsQuery, variables });
  return required(r.data?.listReviewQueueItems, 'listReviewQueueItems');
};

export const fetchReviewItem = async (objectKey: string): Promise<ReviewItemDetail> => {
  const r = await client.graphql({ query: getReviewItemQuery, variables: { objectKey } });
  return required(r.data?.getReviewItem, 'getReviewItem');
};

export const fetchQueuesOverview = async (): Promise<ReviewQueuesOverview> => {
  const r = await client.graphql({ query: getReviewQueuesOverviewQuery });
  return required(r.data?.getReviewQueuesOverview, 'getReviewQueuesOverview');
};

export const saveDraft = async (
  item: ItemRef,
  sectionId: string,
  data: Record<string, unknown>,
  baseRevision: number | null,
): Promise<ReviewDraft> => {
  const r = await client.graphql({
    query: saveReviewDraftMutation,
    variables: { ...ref(item), sectionId, data: JSON.stringify(data), baseRevision },
  });
  return required(r.data?.saveReviewDraft, 'saveReviewDraft');
};

export const completeItem = async (item: ItemRef, checklist?: boolean[], note?: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: completeReviewItemMutation, variables: { ...ref(item), checklist, note } });
  return required(r.data?.completeReviewItem, 'completeReviewItem');
};

export const rejectItem = async (item: ItemRef, reason: string, note?: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: rejectReviewItemMutation, variables: { ...ref(item), reason, note } });
  return required(r.data?.rejectReviewItem, 'rejectReviewItem');
};

export const forceReleaseItem = async (item: ItemRef): Promise<ReviewItem> => {
  const r = await client.graphql({ query: forceReleaseReviewItemMutation, variables: ref(item) });
  return required(r.data?.forceReleaseReviewItem, 'forceReleaseReviewItem');
};

export const reassignItem = async (item: ItemRef, assigneeEmail: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: reassignReviewItemMutation, variables: { ...ref(item), assigneeEmail } });
  return required(r.data?.reassignReviewItem, 'reassignReviewItem');
};

export const moveItem = async (item: ItemRef, targetQueue: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: moveReviewItemMutation, variables: { ...ref(item), targetQueue } });
  return required(r.data?.moveReviewItem, 'moveReviewItem');
};

/** "Low confidence: Employer name 0.62 < 0.80" — one reason's chip text. */
export const reasonText = (reason: {
  kind: string;
  label?: string | null;
  field?: string | null;
  score: number;
  threshold: number;
}): string => {
  const what = reason.kind === 'blueprint_confidence' ? 'Blueprint confidence' : 'Low confidence';
  const name = reason.label || reason.field;
  return `${what}: ${name ? `${name} ` : ''}${reason.score.toFixed(2)} < ${reason.threshold.toFixed(2)}`;
};

/** The file name part of an object key, for compact display. */
export const documentName = (objectKey: string): string => objectKey.split('/').pop() || objectKey;

/** The `CODE` of a review API refusal ("STALE_DRAFT: a newer draft was saved"), or null. */
export const reviewErrorCode = (err: unknown): string | null => {
  const message = extractGraphQLErrorMessage(err);
  const match = /\b([A-Z][A-Z_]{2,}):/.exec(message);
  return match ? match[1] : null;
};
