// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Submits a document for reprocessing (processChanges). Shared by the edit page's Process
// Changes on a direct edit and the document page's Edit Page Class (Save and
// Re-extract), Re-extract Sections, Edit Page Text and Reset Page Class.
import { generateClient } from '../../api/client-shim';
import { processChanges } from '../../graphql/generated';
import type { ModifiedPageInput, ModifiedSectionInput } from '../../graphql/generated/operation-types';

const client = generateClient();

/** How long to wait for the server before reporting a timeout. */
export const PROCESS_CHANGES_TIMEOUT_MS = 30000;

/**
 * Reprocess the document. With no section or page changes, only evaluation and
 * summarization re-run; a modified section is extracted again; a modified page has its
 * text re-read (`textModified`) or the document classified again (`classReset`).
 * `modifiedPages` is sent only when there are some. Throws with the server's message
 * when the request is refused or times out.
 */
export const submitProcessChanges = async (
  objectKey: string,
  modifiedSections: ModifiedSectionInput[] = [],
  modifiedPages: ModifiedPageInput[] = [],
): Promise<void> => {
  const variables = { objectKey, modifiedSections, ...(modifiedPages.length > 0 ? { modifiedPages } : {}) };
  const result = await Promise.race([
    client.graphql({ query: processChanges, variables }),
    new Promise<never>((_, reject) => {
      setTimeout(() => reject(new Error('Request timed out after 30 seconds')), PROCESS_CHANGES_TIMEOUT_MS);
    }),
  ]);
  const response = result.data.processChanges;
  if (!response?.success) {
    throw new Error((response?.message as string) || 'Failed to process changes - no response received');
  }
};
