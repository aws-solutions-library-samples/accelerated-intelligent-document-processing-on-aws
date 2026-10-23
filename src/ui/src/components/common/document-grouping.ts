// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Saves a processed document's page grouping (updateDocumentSections). Shared by the
// document page's Sections panel and the unified editor's Edit Page Grouping.
import { generateClient } from '../../api/client-shim';
import { updateDocumentSections } from '../../graphql/generated';
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
