// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Submits a document for reprocessing (processChanges). Shared by the document page's
// Sections panel and the unified editor's Process Changes on a direct edit.
import { generateClient } from '../../api/client-shim';
import { processChanges } from '../../graphql/generated';
import type { ModifiedSectionInput } from '../../graphql/generated/operation-types';

const client = generateClient();

/** How long to wait for the server before reporting a timeout. */
export const PROCESS_CHANGES_TIMEOUT_MS = 30000;

/**
 * Reprocess the document. With no section changes, only evaluation and summarization
 * re-run. Throws with the server's message when the request is refused or times out.
 */
export const submitProcessChanges = async (objectKey: string, modifiedSections: ModifiedSectionInput[] = []): Promise<void> => {
  const result = await Promise.race([
    client.graphql({ query: processChanges, variables: { objectKey, modifiedSections } }),
    new Promise<never>((_, reject) => {
      setTimeout(() => reject(new Error('Request timed out after 30 seconds')), PROCESS_CHANGES_TIMEOUT_MS);
    }),
  ]);
  const response = result.data.processChanges;
  if (!response?.success) {
    throw new Error((response?.message as string) || 'Failed to process changes - no response received');
  }
};
