// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState, useCallback, useRef } from 'react';
import { generateClient } from '../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, useIntl, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

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
import type { ReprocessDocumentMutationVariables, ReprocessDocumentResult } from '../graphql/generated/operation-types';
import { DOCUMENT_LIST_SHARDS_PER_DAY, LATEST_PERIODS } from '../components/document-list/documents-table-config';
import { Document } from '../types/documents';
import { describeApiError, isNetworkError } from './utils/graphql-error';

// Under the HTTP API transport there are no GraphQL subscriptions; the document
// list is kept fresh by polling instead. AppSync deployments keep using
// subscriptions (real-time) unchanged.
const DOCUMENT_LIST_POLL_INTERVAL_MS = 5000;
// Consecutive background polls that may fail to reach the server before the list
// says so. Error banners do not dismiss themselves, so one dropped request (a
// network blip, a reload of the tab's connection) used to leave "Failed to list
// documents: Failed to fetch" on screen after the very next poll had succeeded.
export const POLL_NETWORK_FAILURES_BEFORE_REPORT = 3;

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

const messages = defineMessages<{
  loadDocumentAction: NoMessageValues;
  listDocumentsAction: NoMessageValues;
  deleteIncomplete: NoMessageValues;
  deleteDocumentsAction: NoMessageValues;
  reprocessFailed: NoMessageValues;
  abortPartial: { readonly abortedCount: number; readonly failedCount: number };
  abortFailed: { readonly errors: string };
  unknownError: NoMessageValues;
  abortWorkflowFailed: NoMessageValues;
}>({
  loadDocumentAction: {
    id: 'documents.api.loadDocumentAction',
    defaultMessage: 'load this document',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when a document cannot be loaded.',
  },
  listDocumentsAction: {
    id: 'documents.api.listDocumentsAction',
    defaultMessage: 'list documents',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the document list cannot be loaded.',
  },
  deleteIncomplete: {
    id: 'documents.api.deleteIncomplete',
    defaultMessage: 'Failed to delete document(s) - the server reported the delete did not complete',
    description: 'Error shown when the server answered a delete of documents without completing it.',
  },
  deleteDocumentsAction: {
    id: 'documents.api.deleteDocumentsAction',
    defaultMessage: 'delete document(s)',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when documents cannot be deleted.',
  },
  reprocessFailed: {
    id: 'documents.api.reprocessFailed',
    defaultMessage: 'Failed to reprocess document(s) - please try again later',
    description: 'Error shown when documents could not be sent for processing again.',
  },
  abortPartial: {
    id: 'documents.api.abortPartial',
    defaultMessage: 'Aborted {abortedCount} document(s), but {failedCount} failed',
    description:
      'Error shown when only some of the documents being processed could be stopped. {abortedCount} is how many were stopped, {failedCount} how many could not be.',
  },
  abortFailed: {
    id: 'documents.api.abortFailed',
    defaultMessage: 'Failed to abort document(s): {errors}',
    description:
      'Error shown when none of the documents being processed could be stopped. {errors} is the reasons the server sent, separated by commas, or Unknown error.',
  },
  unknownError: {
    id: 'documents.api.unknownError',
    defaultMessage: 'Unknown error',
    description: 'Shown in the error of a stop request that failed when the server sent no reason.',
  },
  abortWorkflowFailed: {
    id: 'documents.api.abortWorkflowFailed',
    defaultMessage: 'Failed to abort workflow(s) - please try again later',
    description: 'Error shown when the request to stop processing documents could not be sent or failed.',
  },
});

/** A load's failure: the action phrase and what the call threw. */
interface LoadFailure {
  action: TypedMessageDescriptor<NoMessageValues>;
  cause: unknown;
}

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

/** How getDocumentDetailsFromIds reports a refused read. */
export interface DocumentDetailsOptions {
  /** Raise the shared error banner (default true). */
  reportErrors?: boolean;
  /** Told each key whose read was refused, with the reason. */
  onError?: (objectKey: string, error: unknown) => void;
}

interface UseGraphQlApiReturn {
  documents: Document[];
  isDocumentsListLoading: boolean;
  hasListBeenLoaded: boolean;
  getDocumentDetailsFromIds: (objectKeys: string[], options?: DocumentDetailsOptions) => Promise<Document[]>;
  setIsDocumentsListLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setPeriodsToLoad: React.Dispatch<React.SetStateAction<number>>;
  periodsToLoad: number;
  customDateRange: DateRange | null;
  setCustomDateRange: React.Dispatch<React.SetStateAction<DateRange | null>>;
  documentView: DocumentView;
  setDocumentView: React.Dispatch<React.SetStateAction<DocumentView>>;
  latestTruncated: boolean;
  deleteDocuments: (objectKeys: string[]) => Promise<unknown>;
  reprocessDocuments: ReprocessDocuments;
  abortWorkflows: (objectKeys: string[]) => Promise<unknown>;
}

/**
 * Reprocess documents, optionally under another profile and revision and from a later
 * step. Resolves each document's result (queued, refused or failed, with the reason),
 * or false when the request itself failed, which has then been reported.
 */
export type ReprocessDocuments = (
  objectKeys: string[],
  version?: string,
  revision?: number,
  startStep?: string,
) => Promise<ReprocessDocumentResult[] | false>;

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
  // Only the mutations and the relay below read it: the loads and the poll never do, so a
  // language switch sends no request.
  const intl = useIntl();
  /** A load's failure, held raw until the effect below words it. */
  const [loadFailure, setLoadFailure] = useState<LoadFailure | null>(null);

  useEffect(() => {
    if (!loadFailure) return;
    setErrorMessage(describeApiError(loadFailure.cause, loadFailure.action, intl));
    setLoadFailure(null);
  }, [loadFailure, intl, setErrorMessage]);

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
    async (objectKeys: string[], { reportErrors = true, onError }: DocumentDetailsOptions = {}): Promise<Document[]> => {
      logger.debug('getDocumentDetailsFromIds', objectKeys);
      const getDocumentPromises = objectKeys.map((objectKey) => client.graphql({ query: getDocument, variables: { objectKey } }));
      const getDocumentResolutions = await Promise.allSettled(getDocumentPromises);

      type GetDocumentResolved = Awaited<(typeof getDocumentPromises)[number]>;

      const documentValues = getDocumentResolutions
        .filter((r) => r.status === 'fulfilled')
        .map((r) => (r as PromiseFulfilledResult<GetDocumentResolved>).value?.data?.getDocument)
        .filter((doc): doc is NonNullable<typeof doc> => doc != null) as Document[];

      // `allSettled` + a `fulfilled` filter used to swallow every rejection here,
      // and the detail view renders its panel only `{document && ...}` — so a
      // refused getDocument produced a page with nothing on it and nothing in the
      // console. Report the first rejection instead; the caller still gets whatever
      // resolved, which is what the polling path relies on.
      const firstRejection = getDocumentResolutions.find((r) => r.status === 'rejected');
      // `reportErrors: false` is for a caller that reads many documents and says
      // itself how many it could not, rather than raising the shared banner per call.
      // `onError` is told each key whose read was refused, so a caller can tell a
      // failed read from a document that does not exist (which resolves to nothing).
      getDocumentResolutions.forEach((r, i) => {
        if (r.status === 'rejected') onError?.(objectKeys[i], r.reason);
      });
      if (firstRejection) {
        const reason = (firstRejection as PromiseRejectedResult).reason;
        if (reportErrors) setLoadFailure({ action: messages.loadDocumentAction, cause: reason });
        logger.error('Error fetching document details', reason);
      }

      // Merge the rich detail into shared list state so the open detail view picks
      // up the latest Sections/Pages/Metering (e.g. when a document finishes
      // processing while its detail page is open).
      if (documentValues.length > 0) {
        setDocumentsDeduped(documentValues);
      }

      return documentValues;
    },
    [setDocumentsDeduped],
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
  // Background polls in a row that failed before reaching the server.
  const pollNetworkFailuresRef = useRef<number>(0);

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
  const sendSetDocumentsForDateRange = async (
    dateRange: DateRange | null,
    view: DocumentView,
    { background = false }: { background?: boolean } = {},
  ): Promise<void> => {
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
      pollNetworkFailuresRef.current = 0;
    } catch (error: unknown) {
      setIsDocumentsListLoading(false);
      // A poll the user did not ask for, that never reached the server, is retried
      // by the next tick; only a run of them is worth a banner. Anything the server
      // answered (a 403, a 500) and any load the user started still reports at once.
      if (background && isNetworkError(error)) {
        pollNetworkFailuresRef.current += 1;
        if (pollNetworkFailuresRef.current < POLL_NETWORK_FAILURES_BEFORE_REPORT) {
          logger.warn('Background document refresh did not reach the server; retrying on the next poll', error);
          return;
        }
        // Reported once per run of consecutive failures. A successful poll also resets
        // the count, so a fresh outage after a recovery is reported again.
        pollNetworkFailuresRef.current = 0;
      } else {
        // Anything else breaks the run: the count is of consecutive network failures.
        pollNetworkFailuresRef.current = 0;
      }
      // describeApiError branches on errorType: a 403 gets copy the reader can act
      // on rather than the dispatcher's own words, which name Cognito groups they
      // cannot grant themselves. Everything else still carries the server's detail.
      // The relay effect words it, so this load never reads `intl`.
      setLoadFailure({ action: messages.listDocumentsAction, cause: error });
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
    void sendSetDocumentsForDateRange(resolveDateRange(periodsToLoad, customDateRangeRef.current), documentViewRef.current, {
      background: true,
    });
  }, [periodsToLoad]);

  usePolling(pollDocuments, {
    enabled: true,
    intervalMs: DOCUMENT_LIST_POLL_INTERVAL_MS,
  });

  // ── Mutations ──────────────────────────────────────────────────────────

  // A delete that did not finish has to reach the banner, and it has to say why.
  // `deleteDocument` answers a bare `Boolean!`, the result was only logged at debug
  // level, and the catch arm replaced whatever the server said with "please try again
  // later" — advice that can never work for the shapes this actually fails with, which
  // are deployment faults (a missing permission, a rejected expression) rather than
  // anything a retry clears (#1366). The resolver now raises on a failed delete, so
  // `describeApiError` has a real cause to interpolate; the `=== false` arm below keeps
  // a falsy answer visible too rather than relying on that contract.
  const deleteDocuments = async (objectKeys: string[]): Promise<unknown> => {
    try {
      const result = await client.graphql({ query: deleteDocument, variables: { objectKeys } });
      setIsDocumentsListLoading(true);
      if (result.data.deleteDocument === false) {
        setErrorMessage(intl.formatMessage(messages.deleteIncomplete));
        logger.error('deleteDocument reported failure', objectKeys);
      }
      return result.data.deleteDocument;
    } catch (error) {
      setErrorMessage(describeApiError(error, messages.deleteDocumentsAction, intl));
      logger.error('Error deleting documents', error);
      return false;
    }
  };

  const reprocessDocuments: ReprocessDocuments = async (objectKeys, version, revision, startStep) => {
    try {
      const variables: ReprocessDocumentMutationVariables = { objectKeys };
      if (version) variables.version = version;
      if (revision !== undefined) variables.revision = revision;
      if (startStep) variables.startStep = startStep;
      const result = await client.graphql({ query: reprocessDocument, variables });
      setIsDocumentsListLoading(true);
      return result.data.reprocessDocument.results;
    } catch (error) {
      setErrorMessage(intl.formatMessage(messages.reprocessFailed));
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
        setErrorMessage(
          intl.formatMessage(messages.abortPartial, { abortedCount: response.abortedCount ?? 0, failedCount: response.failedCount ?? 0 }),
        );
      } else if ((response.failedCount ?? 0) > 0 && response.abortedCount === 0) {
        setErrorMessage(
          intl.formatMessage(messages.abortFailed, { errors: response.errors?.join(', ') || intl.formatMessage(messages.unknownError) }),
        );
      }

      return response;
    } catch (error: unknown) {
      setErrorMessage(intl.formatMessage(messages.abortWorkflowFailed));
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
