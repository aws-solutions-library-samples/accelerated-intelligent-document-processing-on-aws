// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState, useCallback, useRef } from 'react';
import { generateClient } from '../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import useAppContext from '../contexts/app';
import usePolling from './use-polling';
import {
  listDocuments,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  getDocumentCount,
  getDocument,
  deleteDocument,
  reprocessDocument,
  abortWorkflow,
} from '../graphql/generated';
import { DOCUMENT_LIST_SHARDS_PER_DAY, LATEST_PERIODS } from '../components/document-list/documents-table-config';
import { Document } from '../types/documents';

// Under the HTTP API transport there are no GraphQL subscriptions; the document
// list is kept fresh by polling instead. AppSync deployments keep using
// subscriptions (real-time) unchanged.
const DOCUMENT_LIST_POLL_INTERVAL_MS = 5000;

/** Rows per listDocuments request (the resolver's MAX_PAGE_SIZE). */
const PAGE_SIZE = 200;

/**
 * How many documents the Latest option aims to load, and the hard ceiling on
 * requests it will spend getting there.
 *
 * Latest is unbounded in time, so it needs a stop condition that the windowed
 * options get for free. Counting *delivered* rows rather than requests is the
 * important part: the resolver applies the reviewer RBAC FilterExpression and the
 * config-version scope filter AFTER DynamoDB's Limit, so a page of 200 can arrive
 * with 3 rows in it. Stopping after a fixed number of pages would silently hand a
 * scoped user a near-empty list and call it "the latest 200".
 *
 * The page ceiling then bounds the pathological case — a caller whose scope
 * matches almost nothing would otherwise walk the whole partition looking for 200
 * survivors. When it bites, the list says so rather than implying completeness.
 */
const LATEST_TARGET_COUNT = 200;
const LATEST_MAX_PAGES = 10;

const client = generateClient();

const logger = new ConsoleLogger('useGraphQlApi');

interface DateRange {
  startDateTime: string;
  endDateTime: string;
}

/**
 * Which submission partition the document list is showing. Test Studio runs its
 * documents through the production pipeline, so they are only distinguishable by
 * the provenance tag the test file copier writes; the backend keys the
 * TypeDateIndex on it (ItemType document vs test-document) and `listDocuments`
 * selects one partition via this argument. The two views are mutually exclusive —
 * there is no combined view, by design, because merging two independently
 * paginated queries would need a synthesised composite nextToken.
 */
export type DocumentView = 'PRODUCTION' | 'TEST';

interface UseGraphQlApiParams {
  initialPeriodsToLoad?: number;
}

interface UseGraphQlApiReturn {
  documents: Document[];
  isDocumentsListLoading: boolean;
  hasListBeenLoaded: boolean;
  getDocumentDetailsFromIds: (objectKeys: string[]) => Promise<Document[]>;
  setIsDocumentsListLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setPeriodsToLoad: React.Dispatch<React.SetStateAction<number>>;
  periodsToLoad: number;
  customDateRange: DateRange | null;
  setCustomDateRange: React.Dispatch<React.SetStateAction<DateRange | null>>;
  documentView: DocumentView;
  setDocumentView: React.Dispatch<React.SetStateAction<DocumentView>>;
  latestTruncated: boolean;
  deleteDocuments: (objectKeys: string[]) => Promise<unknown>;
  reprocessDocuments: (objectKeys: string[], version?: string) => Promise<unknown>;
  abortWorkflows: (objectKeys: string[]) => Promise<unknown>;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
interface DocumentListItem {
  ObjectKey: string;
  PK?: string;
  SK?: string;
  [key: string]: unknown;
}

/**
 * Calculate the date range for a relative time period.
 * periodsToLoad represents the number of shard periods (each 4 hours).
 */
const getDateRangeForPeriod = (periodsToLoad: number): DateRange => {
  const now = new Date();
  const hoursInShard = 24 / DOCUMENT_LIST_SHARDS_PER_DAY;
  const hoursBack = periodsToLoad * hoursInShard;
  const startDate = new Date(now.getTime() - hoursBack * 3600 * 1000);
  return {
    startDateTime: startDate.toISOString(),
    endDateTime: now.toISOString(),
  };
};

/**
 * The range to query, or null for Latest — which means *no* date bounds. The GSI is
 * keyed (ItemType, InitialEventTime) and queried newest-first, so an unbounded
 * query returns the most recent documents regardless of age; the resolver already
 * handles absent start/end times.
 *
 * A custom range always wins, matching how the dropdown clears one when the other
 * is chosen.
 */
export const resolveDateRange = (periodsToLoad: number, customDateRange: DateRange | null): DateRange | null => {
  if (customDateRange) return customDateRange;
  if (periodsToLoad === LATEST_PERIODS) return null;
  return getDateRangeForPeriod(periodsToLoad);
};

// Same default as resolveInitialPeriodsToLoad, so a caller that passes nothing and
// one whose storage is empty land on the same scope. These previously disagreed
// (2 days here, 2 hours there).
const useGraphQlApi = ({ initialPeriodsToLoad = LATEST_PERIODS }: UseGraphQlApiParams = {}): UseGraphQlApiReturn => {
  const [periodsToLoad, setPeriodsToLoad] = useState<number>(initialPeriodsToLoad);
  const [isDocumentsListLoading, setIsDocumentsListLoading] = useState<boolean>(false);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [customDateRange, setCustomDateRange] = useState<DateRange | null>(null);
  // Deliberately NOT persisted to localStorage, unlike periodsToLoad and
  // customDateRange. A sticky view would mean a user who left the list on TEST
  // returns days later, sees none of their uploads, and concludes documents were
  // lost. Every mount starts on the production list.
  const [documentView, setDocumentView] = useState<DocumentView>('PRODUCTION');
  /** Latest stopped with pages remaining, so the list is a prefix of the partition. */
  const [latestTruncated, setLatestTruncated] = useState<boolean>(false);
  const { setErrorMessage } = useAppContext();

  // Ref to track customDateRange in subscription callbacks (closures capture stale state)
  const customDateRangeRef = useRef<DateRange | null>(customDateRange);
  useEffect(() => {
    customDateRangeRef.current = customDateRange;
  }, [customDateRange]);

  // Same reason as customDateRangeRef: the load effect fetches from inside a
  // setTimeout and the poll callback from an interval, so both must read the
  // current view rather than the one captured when they were created.
  const documentViewRef = useRef<DocumentView>(documentView);
  useEffect(() => {
    documentViewRef.current = documentView;
  }, [documentView]);

  const setDocumentsDeduped = useCallback((documentValues: Document[]): void => {
    setDocuments((currentDocuments) => {
      // Build a lookup of existing documents for merging
      const existingByKey: Record<string, Document> = {};
      currentDocuments.forEach((doc) => {
        existingByKey[doc.ObjectKey] = doc;
      });

      // Merge new values with existing documents.
      // Rich data (from subscriptions/getDocument — detected by having 'Sections' key)
      // does a full replacement so reprocessed documents properly clear stale fields.
      // Lightweight list data (from listDocuments — no 'Sections' key) preserves
      // existing non-null fields to avoid wiping rich detail data.
      const mergedNewValues = documentValues.map((newDoc) => {
        const existing = existingByKey[newDoc.ObjectKey];
        if (!existing) return newDoc;

        // Detect rich data: subscription and getDocument responses include 'Sections'
        // (even if empty/null). Lightweight listDocuments responses never include it.
        const isRichData = 'Sections' in newDoc;

        if (isRichData) {
          // Rich data from subscription/getDocument — overlay new fields onto existing.
          // Each subscription event (updateDocument, updateDocumentSection) returns the
          // full DynamoDB item with ALL current Sections/Pages in the correct order.
          // We use the incoming data directly, but protect against null values wiping
          // existing data — updateDocumentStatus events only touch ObjectStatus in DDB
          // and may deliver null for Sections/Pages in the subscription payload.
          const replaced = { ...existing };
          Object.entries(newDoc).forEach(([key, value]) => {
            if (value === undefined) return; // skip truly undefined keys
            // Don't overwrite existing non-null data with null
            // (protects Sections/Pages from being wiped by status-only updates)
            if (value === null && (replaced as Record<string, unknown>)[key] != null) return;
            (replaced as Record<string, unknown>)[key] = value;
          });
          return replaced as Document;
        }

        // Lightweight list data — preserve existing non-null fields
        const merged = { ...existing };
        Object.entries(newDoc).forEach(([key, value]) => {
          if (value !== null && value !== undefined) {
            (merged as Record<string, unknown>)[key] = value;
          }
        });
        return merged as Document;
      });

      // Replace existing docs with merged versions, keep untouched docs
      const mergedIds = new Set(mergedNewValues.map((c) => c.ObjectKey));
      const untouchedDocs = currentDocuments.filter((c) => !mergedIds.has(c.ObjectKey));
      return [...untouchedDocs, ...mergedNewValues];
    });
  }, []);

  /**
   * Fetch full document details for specific documents by ObjectKey.
   * Used both when navigating to the document detail view and by the detail-view
   * poll (which refreshes an open, still-processing document). The rich getDocument
   * responses (including Sections/Pages/Metering) are merged into shared list state
   * via setDocumentsDeduped so the fresh data flows back through the `item` prop and
   * re-renders the detail view — not just returned to the caller.
   */
  const getDocumentDetailsFromIds = useCallback(
    async (objectKeys: string[]): Promise<Document[]> => {
      logger.debug('getDocumentDetailsFromIds', objectKeys);
      const getDocumentPromises = objectKeys.map((objectKey) => client.graphql({ query: getDocument, variables: { objectKey } }));
      const getDocumentResolutions = await Promise.allSettled(getDocumentPromises);

      type GetDocumentResolved = Awaited<(typeof getDocumentPromises)[number]>;

      const documentValues = getDocumentResolutions
        .filter((r) => r.status === 'fulfilled')
        .map((r) => (r as PromiseFulfilledResult<GetDocumentResolved>).value?.data?.getDocument)
        .filter((doc): doc is NonNullable<typeof doc> => doc != null) as Document[];

      // Merge the rich detail into shared list state so the open detail view picks
      // up the latest Sections/Pages/Metering (e.g. when a document finishes
      // processing while its detail page is open).
      if (documentValues.length > 0) {
        setDocumentsDeduped(documentValues);
      }

      return documentValues;
    },
    [setDocumentsDeduped, setErrorMessage],
  );

  // ── Subscriptions ──────────────────────────────────────────────────────
  // Subscriptions use the event data directly — NO getDocument calls needed.
  // The onUpdateDocument subscription includes all Document fields.
  // The onCreateDocument subscription only has ObjectKey, so we add a minimal
  // placeholder that will be updated by the next onUpdateDocument event.

  // AppSync subscriptions (onCreateDocument/onUpdateDocument) have been removed;
  // the document list is kept fresh by polling (see the usePolling block below).

  // ── Document Loading (GSI-based, paginated with cap) ────────────────

  // Track whether the initial list load has been explicitly requested.
  // This prevents listDocuments from firing on mount when the user is on
  // a non-list page (e.g., document details, config). The DocumentList
  // component triggers the initial load by calling setIsDocumentsListLoading(true).
  const hasListBeenRequestedRef = useRef<boolean>(false);

  /**
   * Fetch documents using the GSI-based listDocuments query. A date range drains
   * every page inside it; `null` means Latest, which is unbounded in time and stops
   * once LATEST_TARGET_COUNT rows have been delivered (or LATEST_MAX_PAGES spent).
   * The GSI query returns document list fields directly (no getDocument calls).
   *
   * `view` selects the submission partition. It is threaded as an argument rather
   * than read from state because the callers fire from a setTimeout and an
   * interval, where captured state goes stale.
   */
  const sendSetDocumentsForDateRange = async (dateRange: DateRange | null, view: DocumentView): Promise<void> => {
    const isLatest = dateRange === null;
    try {
      logger.info('Fetching documents via GSI', { ...(dateRange ?? { latest: true }), view });
      let totalLoaded = 0;
      let pagesFetched = 0;
      let currentToken: string | null = null;

      do {
        const variables: Record<string, unknown> = {
          limit: PAGE_SIZE,
          view,
        };
        // Omitted entirely for Latest: the resolver falls back to a bare
        // ItemType key condition, which is newest-first over the whole partition.
        if (dateRange) {
          variables.startDateTime = dateRange.startDateTime;
          variables.endDateTime = dateRange.endDateTime;
        }
        if (currentToken) {
          variables.nextToken = currentToken;
        }
        pagesFetched += 1;

        const response = await client.graphql({
          query: listDocuments,
          variables,
        });

        const result = response.data?.listDocuments;
        const pageDocs = (result?.Documents ?? []) as unknown as Document[];
        currentToken = result?.nextToken ?? null;
        totalLoaded += pageDocs.length;

        // Render incrementally — show each page as it arrives
        if (pageDocs.length > 0) {
          setDocumentsDeduped(pageDocs as unknown as Document[]);
        }

        // Stop loading spinner after first page so user sees results immediately
        if (totalLoaded === pageDocs.length) {
          setIsDocumentsListLoading(false);
        }

        logger.debug(`Fetched ${pageDocs.length} documents (total: ${totalLoaded}), hasMore=${!!currentToken}`);

        if (isLatest && totalLoaded >= LATEST_TARGET_COUNT) {
          logger.info(`Latest: reached ${totalLoaded} documents in ${pagesFetched} page(s)`);
          break;
        }
        if (isLatest && pagesFetched >= LATEST_MAX_PAGES) {
          // Only reachable when server-side filtering discards most of each page,
          // since LATEST_MAX_PAGES * PAGE_SIZE is well above the target otherwise.
          logger.warn(
            `Latest: stopped at ${totalLoaded} documents after the ${LATEST_MAX_PAGES}-page ceiling. ` +
              'Server-side filtering (reviewer scope or config-version scope) is discarding most rows.',
          );
          break;
        }
      } while (currentToken);

      // "More exist than were loaded" — true whichever stop condition fired, and
      // shown in the header counter as "N+" so a capped list never reads as
      // complete. Always assigned, so the marker clears when the user switches to a
      // windowed period or a later run comes back short of the cap.
      setLatestTruncated(isLatest && !!currentToken);

      logger.info(`Total documents loaded: ${totalLoaded}`);
      setIsDocumentsListLoading(false);
    } catch (error: unknown) {
      setIsDocumentsListLoading(false);
      // Extract meaningful error message from GraphQL/Lambda errors
      const gqlError = error as { errors?: { message?: string; errorType?: string }[] };
      const firstError = gqlError?.errors?.[0];
      const detail = firstError?.message || (error instanceof Error ? error.message : 'Unknown error');
      const errorType = firstError?.errorType ? ` (${firstError.errorType})` : '';
      setErrorMessage(`Failed to list documents${errorType}: ${detail}`);
      logger.error('Error fetching documents', error);
    }
  };

  useEffect(() => {
    if (isDocumentsListLoading) {
      // Mark that the list has been requested at least once. This allows
      // the periodsToLoad watcher to auto-reload on subsequent changes.
      hasListBeenRequestedRef.current = true;
      logger.debug('document list is loading');
      setTimeout(() => {
        // The reset matters for the view switch specifically: setDocumentsDeduped
        // merges and keeps rows the response did not mention, so without clearing
        // first, switching away from TEST would leave its documents in the list.
        setDocuments([]);
        sendSetDocumentsForDateRange(resolveDateRange(periodsToLoad, customDateRange), documentViewRef.current);
      }, 1);
    }
  }, [isDocumentsListLoading]);

  useEffect(() => {
    logger.debug('list period changed', periodsToLoad);
    if (!customDateRange && hasListBeenRequestedRef.current) {
      // Only auto-reload when the period changes AFTER the first load was requested
      setIsDocumentsListLoading(true);
    }
  }, [periodsToLoad]);

  useEffect(() => {
    if (customDateRange) {
      logger.debug('custom date range changed', customDateRange);
      setIsDocumentsListLoading(true);
    }
  }, [customDateRange]);

  useEffect(() => {
    logger.debug('document view changed', documentView);
    if (hasListBeenRequestedRef.current) {
      // A different index partition entirely, so this is a full reload rather than
      // a client-side filter of what is already loaded.
      setIsDocumentsListLoading(true);
    }
  }, [documentView]);

  // ── Polling (httpapi transport — replaces onCreate/onUpdate subscriptions) ──
  // Silently re-fetch the active date range on an interval. sendSetDocumentsForDateRange
  // feeds setDocumentsDeduped, whose merge logic preserves rich detail already
  // loaded — so list rows update (status, new docs) without wiping open-document
  // detail. Only runs once the list has actually been requested, and the shared
  // usePolling hook pauses while the tab is hidden.
  const pollDocuments = useCallback(() => {
    if (!hasListBeenRequestedRef.current) return;
    void sendSetDocumentsForDateRange(resolveDateRange(periodsToLoad, customDateRangeRef.current), documentViewRef.current);
  }, [periodsToLoad]);

  usePolling(pollDocuments, {
    enabled: true,
    intervalMs: DOCUMENT_LIST_POLL_INTERVAL_MS,
  });

  // ── Mutations ──────────────────────────────────────────────────────────

  const deleteDocuments = async (objectKeys: string[]): Promise<unknown> => {
    try {
      const result = await client.graphql({ query: deleteDocument, variables: { objectKeys } });
      setIsDocumentsListLoading(true);
      return result.data.deleteDocument;
    } catch (error) {
      setErrorMessage('Failed to delete document(s) - please try again later');
      logger.error('Error deleting documents', error);
      return false;
    }
  };

  const reprocessDocuments = async (objectKeys: string[], version?: string, revision?: number): Promise<unknown> => {
    try {
      const variables: { objectKeys: string[]; version?: string; revision?: number } = { objectKeys };
      if (version) variables.version = version;
      if (revision !== undefined) variables.revision = revision;
      const result = await client.graphql({ query: reprocessDocument, variables });
      setIsDocumentsListLoading(true);
      return result.data.reprocessDocument;
    } catch (error) {
      setErrorMessage('Failed to reprocess document(s) - please try again later');
      logger.error('Error reprocessing documents', error);
      return false;
    }
  };

  const abortWorkflows = async (objectKeys: string[]): Promise<unknown> => {
    try {
      const result = await client.graphql({ query: abortWorkflow, variables: { objectKeys } });
      const response = result.data.abortWorkflow;
      setIsDocumentsListLoading(true);

      if ((response.failedCount ?? 0) > 0 && (response.abortedCount ?? 0) > 0) {
        setErrorMessage(`Aborted ${response.abortedCount ?? 0} document(s), but ${response.failedCount ?? 0} failed`);
      } else if ((response.failedCount ?? 0) > 0 && response.abortedCount === 0) {
        setErrorMessage(`Failed to abort document(s): ${response.errors?.join(', ') || 'Unknown error'}`);
      }

      return response;
    } catch (error: unknown) {
      setErrorMessage('Failed to abort workflow(s) - please try again later');
      logger.error('Error aborting workflows', error);
      return {
        success: false,
        abortedCount: 0,
        failedCount: objectKeys.length,
        errors: [error instanceof Error ? error.message : String(error)],
      };
    }
  };

  return {
    documents,
    isDocumentsListLoading,
    hasListBeenLoaded: hasListBeenRequestedRef.current,
    getDocumentDetailsFromIds,
    setIsDocumentsListLoading,
    setPeriodsToLoad,
    periodsToLoad,
    customDateRange,
    setCustomDateRange,
    documentView,
    setDocumentView,
    latestTruncated,
    deleteDocuments,
    reprocessDocuments,
    abortWorkflows,
  };
};

export default useGraphQlApi;
