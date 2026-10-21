// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Types shared by the review workspace and its adapters (Test Studio annotation and
 * live review queues). An adapter maps its own section and page records onto these.
 */

/** One section of the document under review. */
export interface ReviewSectionRef {
  sectionId: string;
  documentClass?: string | null;
  /** Where the section's result JSON lives (`s3://bucket/key`). */
  resultUri: string;
  /** The latest draft of this section, when one exists (live queues). */
  draftUri?: string | null;
  /**
   * Page ids (1-based, `ReviewPage.Id`) this section covers. Empty means "not known
   * up front": the editor then uses the loaded result's `split_document.page_indices`,
   * or every page.
   */
  pageIds: string[];
}

/** One rendered page image. */
export interface ReviewPage {
  Id: string;
  ImageUri: string;
}

/** A section's result as loaded by an adapter: the parsed object and its raw text. */
export interface LoadedSection {
  data: unknown;
  raw: string;
}
