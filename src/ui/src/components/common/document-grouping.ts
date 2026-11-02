// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Saves a processed document's page grouping (updateDocumentSections). Shared by Edit
// Page Class on the edit page and on the document page.
import { generateClient } from '../../api/client-shim';
import { updateDocumentSections } from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';
import type { GroupedSection } from './section-grouping';

const client = generateClient();

export interface DocumentGroupingResult {
  success: boolean;
  message: string | null;
}

/**
 * Write the grouping. A reasoned refusal (a document mid-pipeline, or in a review queue)
 * comes back as `success: false` with the server's message rather than as an error, so
 * the caller can show it; a transport failure throws.
 */
export const saveDocumentGrouping = async (objectKey: string, sections: GroupedSection[]): Promise<DocumentGroupingResult> => {
  const response = await client.graphql({
    query: updateDocumentSections,
    variables: {
      objectKey,
      sections: sections.map((section) => ({
        sectionId: section.sectionId,
        classification: section.documentClass ?? undefined,
        pageIds: section.pageIds.map((id) => String(id)),
      })),
    },
  });
  const result = response.data?.updateDocumentSections;
  return { success: !!result?.success, message: result?.message ?? null };
};

/**
 * Write the grouping, throwing with the message to show inside the board when it is
 * refused or fails (`describe` words a transport failure). Resolves the server's message.
 */
export const saveDocumentGroupingOrThrow = async (
  objectKey: string,
  sections: GroupedSection[],
  describe: (err: unknown) => string = (err) => `Could not save the page grouping: ${getErrorMessage(err)}`,
): Promise<string | null> => {
  let result: DocumentGroupingResult;
  try {
    result = await saveDocumentGrouping(objectKey, sections);
  } catch (err) {
    throw new Error(describe(err));
  }
  if (!result.success) throw new Error(result.message ?? 'The page grouping could not be saved.');
  return result.message;
};
