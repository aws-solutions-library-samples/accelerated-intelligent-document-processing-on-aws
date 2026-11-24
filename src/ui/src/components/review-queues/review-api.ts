// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Typed calls to the review queue API, shared by the review queue pages.
import { defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

import { generateClient } from '../../api/client-shim';
import { extractGraphQLErrorMessage } from '../../hooks/utils/graphql-error';
import {
  claimNextReviewItem as claimNextReviewItemMutation,
  claimReviewItem as claimReviewItemMutation,
  completeReviewItem as completeReviewItemMutation,
  forceReleaseReviewItem as forceReleaseReviewItemMutation,
  getMyReviewQueues as getMyReviewQueuesQuery,
  getReviewItem as getReviewItemQuery,
  getReviewQueuesOverview as getReviewQueuesOverviewQuery,
  listReviewQueueItems as listReviewQueueItemsQuery,
  moveReviewItem as moveReviewItemMutation,
  reassignReviewItem as reassignReviewItemMutation,
  releaseReviewItem as releaseReviewItemMutation,
  removeReviewItem as removeReviewItemMutation,
  saveReviewDraft as saveReviewDraftMutation,
  takeOverReviewItem as takeOverReviewItemMutation,
} from '../../graphql/generated';
import type {
  ClaimNextResult,
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

/** Claim an open item: it is assigned to the caller (no expiry) and shows in Home › Tasks. */
export const claimItem = async (item: ItemRef): Promise<ReviewItem> => {
  const r = await client.graphql({ query: claimReviewItemMutation, variables: ref(item) });
  return required(r.data?.claimReviewItem, 'claimReviewItem');
};

/** Claim the next open item of a queue; `item` is null with a `reason` (EMPTY, CONTENDED, CAP_REACHED) when none was claimed. */
export const claimNextItem = async (queueName: string): Promise<ClaimNextResult> => {
  const r = await client.graphql({ query: claimNextReviewItemMutation, variables: { queueName } });
  return required(r.data?.claimNextReviewItem, 'claimNextReviewItem');
};

/**
 * Skip: give a held item back to its queue, unassigned (the draft is kept). Holder only.
 * `reason` comes from the queue's skip list; it and `note` go into the item's history.
 */
export const releaseItem = async (item: ItemRef, reason: string, note?: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: releaseReviewItemMutation, variables: { ...ref(item), reason, note } });
  return required(r.data?.releaseReviewItem, 'releaseReviewItem');
};

export const forceReleaseItem = async (item: ItemRef): Promise<ReviewItem> => {
  const r = await client.graphql({ query: forceReleaseReviewItemMutation, variables: ref(item) });
  return required(r.data?.forceReleaseReviewItem, 'forceReleaseReviewItem');
};

export const reassignItem = async (item: ItemRef, assigneeEmail: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: reassignReviewItemMutation, variables: { ...ref(item), assigneeEmail } });
  return required(r.data?.reassignReviewItem, 'reassignReviewItem');
};

/** Take over an item assigned to someone else (Admin): it is assigned to the caller, draft and all. */
export const takeOverItem = async (item: ItemRef): Promise<ReviewItem> => {
  const r = await client.graphql({ query: takeOverReviewItemMutation, variables: ref(item) });
  return required(r.data?.takeOverReviewItem, 'takeOverReviewItem');
};

export const moveItem = async (item: ItemRef, targetQueue: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: moveReviewItemMutation, variables: { ...ref(item), targetQueue } });
  return required(r.data?.moveReviewItem, 'moveReviewItem');
};

/** Take an item out of review (Admin): the document completes with its machine results. */
export const removeItem = async (item: ItemRef, note?: string): Promise<ReviewItem> => {
  const r = await client.graphql({ query: removeReviewItemMutation, variables: { ...ref(item), note: note || undefined } });
  return required(r.data?.removeReviewItem, 'removeReviewItem');
};

/** The `kind` of a reason for a required field that has no extracted value. */
export const MISSING_REQUIRED_KIND = 'missing_required';

/**
 * The `kind` of a reason for a section whose extraction failed and left it with no
 * fields (for example, a content filter withheld the model's reply). Its `label` is
 * the section's processing-issue message, and it names no field.
 */
export const EXTRACTION_FAILED_KIND = 'extraction_failed';

const messages = defineMessages<{
  extractionFailed: NoMessageValues;
  missingRequired: NoMessageValues;
  missingRequiredField: { readonly name: string };
  lowConfidence: { readonly score: number; readonly threshold: number };
  lowConfidenceField: { readonly field: string; readonly score: number; readonly threshold: number };
  blueprintConfidence: { readonly score: number; readonly threshold: number };
  blueprintConfidenceField: { readonly field: string; readonly score: number; readonly threshold: number };
}>({
  extractionFailed: {
    id: 'reviewQueues.reasons.extractionFailed',
    defaultMessage: 'Extraction failed',
    description:
      'Chip text of a review reason for a section whose extraction failed and left it with no extracted data (for example, a content filter withheld the model reply). Also the Reason column of a work list.',
  },
  missingRequired: {
    id: 'reviewQueues.reasons.missingRequired',
    defaultMessage: 'Missing required',
    description:
      'Chip text of a review reason for a required field that has no extracted value, when the field has no name. Also the Top reason column of a work list.',
  },
  missingRequiredField: {
    id: 'reviewQueues.reasons.missingRequiredField',
    defaultMessage: 'Missing required: {name}',
    description:
      "Chip text of a review reason for a required field that has no extracted value. Also the Top reason column of a work list. {name} is the field's label or name, from the document class schema.",
  },
  lowConfidence: {
    id: 'reviewQueues.reasons.lowConfidence',
    defaultMessage: 'Low confidence: {score, number, ::.00} < {threshold, number, ::.00}',
    description:
      'Chip text of a review reason for an extracted value whose confidence is below its threshold, when the field has no name. Also the Top reason column of a work list. {score} is the confidence and {threshold} the threshold, both from 0 to 1 with two decimals.',
  },
  lowConfidenceField: {
    id: 'reviewQueues.reasons.lowConfidenceField',
    defaultMessage: 'Low confidence: {field} {score, number, ::.00} < {threshold, number, ::.00}',
    description:
      "Chip text of a review reason for an extracted value whose confidence is below its threshold. Also the Top reason column of a work list. {field} is the field's label or name, from the document class schema; {score} is the confidence and {threshold} the threshold, both from 0 to 1 with two decimals.",
  },
  blueprintConfidence: {
    id: 'reviewQueues.reasons.blueprintConfidence',
    defaultMessage: 'Blueprint confidence: {score, number, ::.00} < {threshold, number, ::.00}',
    description:
      "Chip text of a review reason for a section whose matched Bedrock Data Automation blueprint has a confidence below the review threshold. Also the Top reason column of a work list. {score} is the blueprint's confidence and {threshold} the threshold, both from 0 to 1 with two decimals.",
  },
  blueprintConfidenceField: {
    id: 'reviewQueues.reasons.blueprintConfidenceField',
    defaultMessage: 'Blueprint confidence: {field} {score, number, ::.00} < {threshold, number, ::.00}',
    description:
      "Chip text of a review reason for a matched Bedrock Data Automation blueprint whose confidence is below the review threshold, when the reason names a field. Also the Top reason column of a work list. {field} is the field's label or name; {score} is the confidence and {threshold} the threshold, both from 0 to 1 with two decimals.",
  },
});

/**
 * One reason's chip text, in the UI's language: "Low confidence: Employer name 0.62 < 0.80",
 * "Missing required: Employer name" for a required field with no value, or
 * "Extraction failed" for a section with no extracted data.
 */
export const reasonText = (
  reason: {
    kind: string;
    label?: string | null;
    field?: string | null;
    score: number;
    threshold: number;
  },
  intl: Pick<IntlShape, 'formatMessage'>,
): string => {
  const name = reason.label || reason.field;
  if (reason.kind === EXTRACTION_FAILED_KIND) return intl.formatMessage(messages.extractionFailed);
  if (reason.kind === MISSING_REQUIRED_KIND) {
    return name ? intl.formatMessage(messages.missingRequiredField, { name }) : intl.formatMessage(messages.missingRequired);
  }
  // Pre-rounded with toFixed(2) to keep today's digits: Intl alone rounds 0.615 to 0.62, toFixed to 0.61.
  const scores = { score: Number(reason.score.toFixed(2)), threshold: Number(reason.threshold.toFixed(2)) };
  if (reason.kind === 'blueprint_confidence') {
    return name
      ? intl.formatMessage(messages.blueprintConfidenceField, { field: name, ...scores })
      : intl.formatMessage(messages.blueprintConfidence, scores);
  }
  return name
    ? intl.formatMessage(messages.lowConfidenceField, { field: name, ...scores })
    : intl.formatMessage(messages.lowConfidence, scores);
};

/** The file name part of an object key, for compact display. */
export const documentName = (objectKey: string): string => objectKey.split('/').pop() || objectKey;

/** The `CODE` of a review API refusal ("STALE_DRAFT: a newer draft was saved"), or null. */
export const reviewErrorCode = (err: unknown): string | null => {
  const message = extractGraphQLErrorMessage(err);
  const match = /\b([A-Z][A-Z_]{2,}):/.exec(message);
  return match ? match[1] : null;
};
