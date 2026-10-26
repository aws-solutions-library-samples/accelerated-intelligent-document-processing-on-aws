// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { Document } from '../../types/documents';

/**
 * How many completed documents "Populate tokens from Documents" fetches in full.
 *
 * The Document List's rows carry no `Metering` and no `Sections` — the list query
 * does not select them, to keep the list cheap — so every row reads as "no metering
 * data" and Populate reported "No Metering Data Found" beside a list of completed
 * documents. The token counts and the section classes Populate needs come only from
 * `getDocument`, one request each, so it reads the most recent completed documents
 * rather than all of them.
 */
export const MAX_DOCUMENTS_TO_DETAIL = 25;

type ListRow = Pick<Document, 'ObjectKey' | 'ObjectStatus'> & Partial<Pick<Document, 'Metering' | 'CompletionTime' | 'InitialEventTime'>>;

/** Keys of the most recent completed documents whose metering has not been loaded. */
export const documentsNeedingDetail = (documents: ListRow[], limit = MAX_DOCUMENTS_TO_DETAIL): string[] =>
  documents
    .filter((doc) => doc.ObjectStatus === 'COMPLETED' && !doc.Metering)
    .sort((a, b) =>
      String(b.CompletionTime ?? b.InitialEventTime ?? '').localeCompare(String(a.CompletionTime ?? a.InitialEventTime ?? '')),
    )
    .slice(0, limit)
    .map((doc) => String(doc.ObjectKey));

/** `documents` with each row replaced by its full record where one was fetched. */
export const withDetails = <T extends { ObjectKey: string }>(documents: T[], detailed: T[]): T[] => {
  const byKey = new Map(detailed.map((doc) => [doc.ObjectKey, doc]));
  return documents.map((doc) => byKey.get(doc.ObjectKey) ?? doc);
};
