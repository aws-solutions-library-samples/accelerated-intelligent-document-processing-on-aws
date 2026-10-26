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
    // The API's timestamps are ISO-8601, which sort chronologically as strings.
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

/**
 * How many full records are requested at once. The same bound the Document List
 * uses when it hydrates rows (its HYDRATION_CHUNK_SIZE), so one button press cannot
 * become a burst of simultaneous API calls that the API throttles.
 */
export const DETAIL_FETCH_CHUNK_SIZE = 5;

/** Fetch `keys` in rounds of `chunkSize`, concatenating what each round returns. */
export const fetchInChunks = async <T>(
  keys: string[],
  fetchDetails: (chunk: string[]) => Promise<T[]>,
  chunkSize = DETAIL_FETCH_CHUNK_SIZE,
): Promise<T[]> => {
  const results: T[] = [];
  for (let i = 0; i < keys.length; i += chunkSize) {
    results.push(...(await fetchDetails(keys.slice(i, i + chunkSize))));
  }
  return results;
};

/** Completed documents without metering beyond the ones a single Populate reads. */
export const documentsBeyondCap = (documents: ListRow[], limit = MAX_DOCUMENTS_TO_DETAIL): number =>
  Math.max(0, documents.filter((doc) => doc.ObjectStatus === 'COMPLETED' && !doc.Metering).length - limit);
