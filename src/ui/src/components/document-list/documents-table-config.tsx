// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import {
  Button,
  ButtonDropdown,
  CollectionPreferences,
  Link,
  SegmentedControl,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import type { ButtonDropdownProps, CollectionPreferencesProps } from '@cloudscape-design/components';
import type { TableProps } from '@cloudscape-design/components';

import { isBaselineAvailable, type ExportScope } from '../document-panel/document-export';

import { TableHeader } from '../common/table';
import { DOCUMENTS_PATH, testRunResultsHref } from '../../routes/constants';
import type { DocumentView } from '../../contexts/documents';
import { renderHitlStatus } from '../common/hitl-status-renderer';
import CircuitBreakerBadge from './CircuitBreakerBadge';
import { formatConfigVersionLink } from '../test-studio/utils/configVersionUtils';
import type { ConfigVersion } from '../test-studio/utils/configVersionUtils';

export type { ConfigVersion };

export interface MappedDocument {
  objectKey: string;
  objectStatus: string;
  initialEventTime: string;
  completionTime: string;
  duration: string;
  configVersion: string;
  configRevision?: number | null;
  evaluationStatus: string;
  confidenceAlertCount: number;
  processingIssueCount: number;
  hitlStatus: string;
  hitlReviewOwner: string;
  hitlReviewOwnerEmail: string;
  hitlReviewedBy: string;
  hitlReviewedByEmail: string;
  hitlTriggered: boolean;
  hitlCompleted: boolean;
  [key: string]: unknown;
}

interface TimePeriodConfig {
  count: number;
  text: string;
}

interface DocumentsPreferencesProps {
  preferences: CollectionPreferencesProps.Preferences;
  setPreferences: (prefs: CollectionPreferencesProps.Preferences) => void;
  disabled?: boolean;
  pageSizeOptions?: CollectionPreferencesProps.PageSizePreference['options'];
  visibleContentOptions?: CollectionPreferencesProps.VisibleContentPreference['options'];
}

interface DocumentsCommonHeaderProps {
  resourceName?: string;
  selectedItems?: readonly MappedDocument[];
  onDelete?: (() => void) | null;
  onReprocess?: (() => void) | null;
  onAbort?: (() => void) | null;
  onClaimReview?: (() => void) | null;
  onReleaseReview?: (() => void) | null;
  currentUsername?: string;
  loading?: boolean;
  setIsLoading?: (loading: boolean) => void;
  periodsToLoad?: number;
  setPeriodsToLoad?: (periods: number) => void;
  customDateRange?: { startDateTime: string; endDateTime: string } | null;
  setCustomDateRange?: (range: { startDateTime: string; endDateTime: string } | null) => void;
  onCustomDateRange?: () => void;
  downloadToExcel?: () => void;
  /** Rows currently passing the filter — used for the Excel item's row count. */
  totalItems?: unknown[];
  /** Starts a bulk artifact export (ZIP) for the selected documents. */
  onDownloadSelected?: ((scope: ExportScope) => void) | null;
  isDownloadInProgress?: boolean;
  /**
   * When set, Abort / Reprocess / Delete render disabled with this as their
   * tooltip. Used by the Test Studio view, where mutating a run's documents would
   * invalidate the results scored against them. Disabled-with-a-reason rather than
   * hidden, so the constraint is discoverable instead of the buttons just vanishing.
   */
  destructiveDisabledReason?: string | null;
  [key: string]: unknown;
}

/**
 * Two download actions live in one menu, split into labeled groups because they
 * differ in scope: the Excel export covers every filtered row, while the ZIP
 * exports cover only the selected documents. The Excel item stays enabled while a
 * ZIP export runs — only the ZIP scopes are held.
 */
export const buildDownloadMenuItems = (
  selectedItems: readonly MappedDocument[],
  filteredCount: number,
  includeZipScopes = true,
  isDownloadInProgress = false,
): ButtonDropdownProps.ItemOrGroup[] => {
  const selectedCount = selectedItems.length;
  const noSelection = selectedCount === 0;
  const blockedReason = (() => {
    if (isDownloadInProgress) return 'A download is already in progress';
    if (noSelection) return 'Select one or more documents in the table';
    return undefined;
  })();
  const zipItem = (id: ExportScope, text: string, extraDisabled?: string): ButtonDropdownProps.Item => ({
    id,
    text,
    iconName: 'download',
    disabled: !!blockedReason || !!extraDisabled,
    disabledReason: blockedReason ?? extraDisabled,
  });

  const listGroup: ButtonDropdownProps.ItemOrGroup = {
    text: 'Document list',
    items: [
      {
        id: 'excel',
        text: `Table as Excel (${filteredCount} ${filteredCount === 1 ? 'row' : 'rows'})`,
        iconName: 'download',
      },
    ],
  };
  if (!includeZipScopes) return [listGroup];

  return [
    listGroup,
    {
      text: `Selected documents (${selectedCount})`,
      items: [
        zipItem('all', 'All data (ZIP)'),
        zipItem('predictions', 'Predictions (ZIP)'),
        zipItem(
          'baselines',
          'Baselines (ZIP)',
          !noSelection && !selectedItems.some(isBaselineAvailable) ? 'No selected document has an evaluation baseline' : undefined,
        ),
      ],
    },
  ];
};

export const KEY_COLUMN_ID = 'objectKey';
export const UNIQUE_TRACK_ID = 'uniqueId';
export const TEST_RUN_COLUMN_ID = 'testRunId';

/**
 * Test Studio copies a run's inputs into the pipeline under a `{testRunId}/`
 * prefix, so the run id is recoverable from the object key. Mirrors
 * `_TEST_RUN_KEY_RE` in src/lambda/backfill_gsi_attributes/index.py — the shape is
 * `<testSetId>-<YYYYMMDD>-<HHMMSS>/`.
 *
 * This is only used to label and link rows already known to be test submissions
 * (the caller queried the TEST view), never to decide whether a row *is* one. That
 * distinction matters: an ordinary upload can coincidentally share the shape, which
 * is why the backend requires a matching testrun record before retyping anything.
 */
const TEST_RUN_KEY_RE = /^([^/]+-\d{8}-\d{6})\//;

/** The test run id embedded in a test submission's object key, or null. */
export const testRunIdFromObjectKey = (objectKey: string | undefined): string | null => TEST_RUN_KEY_RE.exec(objectKey ?? '')?.[1] ?? null;

const TEST_RUN_COLUMN: TableProps.ColumnDefinition<MappedDocument> = {
  id: TEST_RUN_COLUMN_ID,
  header: 'Test Run',
  cell: (item) => {
    const testRunId = testRunIdFromObjectKey(item.objectKey);
    if (!testRunId) return '-';
    return <Link href={testRunResultsHref(testRunId)}>{testRunId}</Link>;
  },
  sortingComparator: (a, b) => (testRunIdFromObjectKey(a.objectKey) ?? '').localeCompare(testRunIdFromObjectKey(b.objectKey) ?? ''),
  width: 260,
};

export const COLUMN_DEFINITIONS_MAIN = (
  versions: ConfigVersion[] = [],
  documentView: DocumentView = 'PRODUCTION',
): TableProps.ColumnDefinition<MappedDocument>[] => [
  {
    id: KEY_COLUMN_ID,
    header: 'Document ID',
    cell: (item) => {
      // Double-encode to ensure slashes remain encoded after page refresh
      const safeObjectKey = encodeURIComponent(item.objectKey);
      return <Link href={`#${DOCUMENTS_PATH}/${safeObjectKey}`}>{item.objectKey}</Link>;
    },
    sortingField: 'objectKey',
    width: 300,
  },
  // Second, not first: Document ID stays the leftmost identifier. Column order
  // comes from this array — `visibleColumns` only controls visibility.
  ...(documentView === 'TEST' ? [TEST_RUN_COLUMN] : []),
  {
    id: 'objectStatus',
    header: 'Status',
    cell: (item) => item.objectStatus,
    sortingField: 'objectStatus',
    width: 150,
  },
  {
    id: 'configVersion',
    header: 'Config Profile',
    cell: (item) =>
      formatConfigVersionLink(
        item.configVersion,
        versions as unknown as ConfigVersion[],
        undefined,
        item.configRevision as number | null | undefined,
      ),
    sortingField: 'configVersion',
    width: 150,
  },
  {
    id: 'initialEventTime',
    header: 'Submitted',
    cell: (item) => item.initialEventTime,
    sortingField: 'initialEventTime',
    width: 225,
  },
  {
    id: 'completionTime',
    header: 'Completed',
    cell: (item) => item.completionTime,
    sortingField: 'completionTime',
    width: 225,
  },
  {
    id: 'duration',
    header: 'Duration',
    cell: (item) => item.duration,
    sortingField: 'duration',
    width: 150,
  },
  {
    id: 'evaluationStatus',
    header: 'Evaluation',
    cell: (item) => item.evaluationStatus || 'N/A',
    sortingField: 'evaluationStatus',
    width: 150,
  },
  {
    id: 'confidenceAlertCount',
    header: 'Confidence Alerts',
    cell: (item) => item.confidenceAlertCount || 0,
    sortingField: 'confidenceAlertCount',
    width: 150,
  },
  {
    id: 'processingIssueCount',
    header: 'Processing Issues',
    cell: (item) => {
      const count = item.processingIssueCount || 0;
      return count === 0 ? <StatusIndicator type="success">0</StatusIndicator> : <StatusIndicator type="warning">{count}</StatusIndicator>;
    },
    sortingField: 'processingIssueCount',
    width: 150,
  },
  {
    id: 'hitlStatus',
    header: 'Review Status',
    cell: (item) => renderHitlStatus(item),
    sortingField: 'hitlStatus',
    width: 150,
  },
  {
    id: 'hitlReviewOwner',
    header: 'Review Owner',
    cell: (item) => item.hitlReviewOwnerEmail || item.hitlReviewOwner || '-',
    sortingField: 'hitlReviewOwner',
    width: 180,
  },
  {
    id: 'hitlReviewedBy',
    header: 'Review Completed By',
    cell: (item) => item.hitlReviewedByEmail || item.hitlReviewedBy || '-',
    sortingField: 'hitlReviewedBy',
    width: 180,
  },
];

export const DEFAULT_SORT_COLUMN = { sortingField: 'initialEventTime' };

export const SELECTION_LABELS = {
  itemSelectionLabel: (_data: unknown, row: MappedDocument) => `select ${row.objectKey}`,
  allItemsSelectionLabel: () => 'select all',
  selectionGroupLabel: 'Document selection',
};

const PAGE_SIZE_OPTIONS = [
  { value: 10, label: '10 Documents' },
  { value: 30, label: '30 Documents' },
  { value: 50, label: '50 Documents' },
];

const VISIBLE_CONTENT_OPTIONS = [
  {
    label: 'Document list properties',
    options: [
      { id: 'objectKey', label: 'Document ID', editable: false },
      { id: 'objectStatus', label: 'Status' },
      { id: 'initialEventTime', label: 'Submitted' },
      { id: 'completionTime', label: 'Completed' },
      { id: 'duration', label: 'Duration' },
      { id: 'configVersion', label: 'Config Profile' },
      { id: 'evaluationStatus', label: 'Evaluation' },
      { id: 'confidenceAlertCount', label: 'Confidence Alerts' },
      { id: 'processingIssueCount', label: 'Processing Issues' },
      { id: 'hitlStatus', label: 'Review Status' },
      { id: 'hitlReviewOwner', label: 'Review Owner' },
      { id: 'hitlReviewedBy', label: 'Review Completed By' },
    ],
  },
];

const VISIBLE_CONTENT = [
  'objectKey',
  'objectStatus',
  'configVersion',
  'initialEventTime',
  'duration',
  'confidenceAlertCount',
  'processingIssueCount',
  'hitlStatus',
];

export const DEFAULT_PREFERENCES = {
  pageSize: PAGE_SIZE_OPTIONS[0].value,
  visibleContent: VISIBLE_CONTENT,
  wrapLines: false,
};

export const DocumentsPreferences = ({
  preferences,
  setPreferences,
  disabled,
  pageSizeOptions = PAGE_SIZE_OPTIONS,
  visibleContentOptions = VISIBLE_CONTENT_OPTIONS,
}: DocumentsPreferencesProps): React.JSX.Element => (
  <CollectionPreferences
    title="Preferences"
    confirmLabel="Confirm"
    cancelLabel="Cancel"
    disabled={disabled}
    preferences={preferences}
    onConfirm={({ detail }) => setPreferences(detail)}
    pageSizePreference={{
      title: 'Page size',
      options: pageSizeOptions,
    }}
    wrapLinesPreference={{
      label: 'Wrap lines',
      description: 'Check to see all the text and wrap the lines',
    }}
    visibleContentPreference={{
      title: 'Select visible columns',
      options: visibleContentOptions,
    }}
  />
);

// number of shards per day used by the list documents API
export const DOCUMENT_LIST_SHARDS_PER_DAY = 6;

/**
 * `periodsToLoad` sentinel for the Latest option: fetch the newest documents with
 * no date bounds at all, rather than everything inside a window.
 *
 * A sentinel in the same value the other options set keeps one code path for
 * persistence, the dropdown's display text, and the reload-on-change effect. It is
 * negative so it cannot collide with a real shard count — but note
 * `getInitialPeriodsToLoad` in GenAIIDPLayout takes `Math.abs` of the stored value,
 * so it has to be recognised there before that runs.
 */
export const LATEST_PERIODS = -2;

/** Widest window the stored preference may name, in shard periods (30 days). */
const MAX_STORED_PERIODS = DOCUMENT_LIST_SHARDS_PER_DAY * 30;

/**
 * Resolve the list's initial load scope from whatever is in localStorage.
 *
 * Defaults to Latest rather than a window: with a window, a stack that has been
 * quiet longer than the window opens on an empty table, which reads as a broken
 * deployment rather than as "nothing recent". Latest is empty only when the stack
 * genuinely holds no documents. It matters most for the Reviewer role, whose queue
 * is age-agnostic — a document pending review for three days was invisible under
 * the previous 2-hour default.
 *
 * Only reachable when nothing is stored, so this changes first-run behaviour
 * (fresh deployment, new browser, cleared storage) and leaves anyone with a saved
 * preference on it.
 */
export const resolveInitialPeriodsToLoad = (stored: unknown): number => {
  // Checked before the Math.abs below, which would read the negative Latest
  // sentinel back as a plausible-looking 2 shard periods (8 hours).
  if (stored === LATEST_PERIODS) return LATEST_PERIODS;

  const periods = Math.abs(Number(stored));
  if (!Number.isFinite(periods) || periods > MAX_STORED_PERIODS) return LATEST_PERIODS;
  // 0 covers both an absent key and an explicit 0, neither of which names a window.
  return periods > 0 ? periods : LATEST_PERIODS;
};

const TIME_PERIOD_DROPDOWN_CONFIG: Record<string, TimePeriodConfig> = {
  // First, because "the most recent work" is the most common intent and the
  // window options cannot express it: before this, a list with nothing in the last
  // 30 days was empty with no option left to widen to.
  'load-latest': { count: LATEST_PERIODS, text: 'Latest' },
  'refresh-2h': { count: 0.5, text: '2 hrs' },
  'refresh-4h': { count: 1, text: '4 hrs' },
  'refresh-8h': { count: DOCUMENT_LIST_SHARDS_PER_DAY / 3, text: '8 hrs' },
  'refresh-1d': { count: DOCUMENT_LIST_SHARDS_PER_DAY, text: '1 day' },
  'refresh-2d': { count: 2 * DOCUMENT_LIST_SHARDS_PER_DAY, text: '2 days' },
  'refresh-1w': { count: 7 * DOCUMENT_LIST_SHARDS_PER_DAY, text: '1 week' },
  'refresh-2w': { count: 14 * DOCUMENT_LIST_SHARDS_PER_DAY, text: '2 weeks' },
  'refresh-1m': { count: 30 * DOCUMENT_LIST_SHARDS_PER_DAY, text: '30 days' },
  'custom-range': { count: -1, text: 'Custom range...' },
};
const TIME_PERIOD_DROPDOWN_ITEMS = Object.keys(TIME_PERIOD_DROPDOWN_CONFIG).map((k) => ({
  id: k,
  ...TIME_PERIOD_DROPDOWN_CONFIG[k],
}));

// local storage key to persist the last periods to load
export const PERIODS_TO_LOAD_STORAGE_KEY = 'periodsToLoad';

// local storage key to persist custom date range
export const CUSTOM_DATE_RANGE_STORAGE_KEY = 'customDateRange';

// Statuses that can be aborted
const ABORTABLE_STATUSES = [
  'QUEUED',
  'RUNNING',
  'PREPROCESSING',
  'OCR',
  'CLASSIFYING',
  'EXTRACTING',
  'ASSESSING',
  'POSTPROCESSING',
  'HITL_IN_PROGRESS',
  'SUMMARIZING',
  'EVALUATING',
];

/**
 * Switches the list between the two submission partitions. A two-state control
 * rather than a "show test documents too" toggle because the backend has no
 * combined view, and because the populations differ by orders of magnitude — on a
 * stack that runs Test Studio regularly the test documents outnumber real uploads
 * ~50:1, so merging them would bury the uploads and undo the point of separating
 * them in the first place.
 */
export const DocumentViewSelector = ({
  documentView,
  setDocumentView,
  disabled,
}: {
  documentView: DocumentView;
  setDocumentView: (view: DocumentView) => void;
  disabled?: boolean;
}): React.JSX.Element => (
  <SegmentedControl
    selectedId={documentView}
    onChange={({ detail }) => setDocumentView(detail.selectedId as DocumentView)}
    label="Document source"
    options={[
      // "Production" rather than "Uploads": API, CLI and extension submissions land
      // in this partition too, not just documents uploaded through the web UI.
      { id: 'PRODUCTION', text: 'Production', disabled },
      { id: 'TEST', text: 'Test Studio', disabled },
    ]}
  />
);

export const DocumentsCommonHeader = ({
  resourceName = 'Documents',
  selectedItems = [],
  onDelete,
  onReprocess,
  onAbort,
  onClaimReview,
  onReleaseReview,
  onDownloadSelected,
  isDownloadInProgress,
  destructiveDisabledReason,
  _currentUsername,
  ...props
}: DocumentsCommonHeaderProps): React.JSX.Element => {
  const onPeriodToLoadChange = ({ detail }: { detail: { id: string } }) => {
    const { id } = detail;
    if (id === 'custom-range') {
      // Signal parent to show date range picker
      if (props.onCustomDateRange) {
        props.onCustomDateRange();
      }
      return;
    }
    const shardCount = TIME_PERIOD_DROPDOWN_CONFIG[id].count;
    // Clear any custom date range when switching to relative period
    if (props.setCustomDateRange) {
      props.setCustomDateRange(null);
      localStorage.removeItem(CUSTOM_DATE_RANGE_STORAGE_KEY);
    }
    props.setPeriodsToLoad?.(shardCount);
    localStorage.setItem(PERIODS_TO_LOAD_STORAGE_KEY, JSON.stringify(shardCount));
  };

  // Determine display text
  const getDisplayText = (): string => {
    if (props.customDateRange) {
      const start = new Date(props.customDateRange.startDateTime);
      const end = new Date(props.customDateRange.endDateTime);
      const formatDate = (d: Date): string =>
        `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
      return `${formatDate(start)} → ${formatDate(end)}`;
    }
    return TIME_PERIOD_DROPDOWN_ITEMS.filter((i) => i.count === props.periodsToLoad)[0]?.text || '';
  };

  const periodText = getDisplayText();

  const hasSelectedItems = selectedItems.length > 0;
  // Check if any selected items can be aborted
  const hasAbortableItems = selectedItems.some((item) => ABORTABLE_STATUSES.includes(item.objectStatus));
  // Check if any selected items can be claimed (pending HITL and not owned)
  const hasClaimableItems = selectedItems.some((item) => item.hitlTriggered && !item.hitlCompleted && !item.hitlReviewOwner);
  // Check if any selected items can be released (has review owner and HITL not completed)
  const hasReleasableItems = selectedItems.some((item) => item.hitlReviewOwner && !item.hitlCompleted);

  const downloadMenuItems = buildDownloadMenuItems(
    selectedItems,
    props.totalItems?.length ?? 0,
    !!onDownloadSelected,
    !!isDownloadInProgress,
  );

  const onDownloadItemClick = ({ detail }: { detail: { id: string } }) => {
    if (detail.id === 'excel') {
      props.downloadToExcel?.();
      return;
    }
    onDownloadSelected?.(detail.id as ExportScope);
  };

  return (
    <TableHeader
      title={resourceName}
      actionButtons={
        <SpaceBetween size="xxs" direction="horizontal">
          <ButtonDropdown loading={props.loading} onItemClick={onPeriodToLoadChange} items={TIME_PERIOD_DROPDOWN_ITEMS}>
            {`Load: ${periodText}`}
          </ButtonDropdown>
          <span title="Refresh document list">
            <Button
              iconName="refresh"
              variant="normal"
              loading={props.loading}
              onClick={() => props.setIsLoading?.(true)}
              ariaLabel="Refresh"
            />
          </span>
          <ButtonDropdown
            items={downloadMenuItems}
            onItemClick={onDownloadItemClick}
            loading={props.loading}
            expandToViewport
            ariaLabel="Download"
          >
            Download
          </ButtonDropdown>
          {onClaimReview && (
            <Button variant="primary" disabled={!hasClaimableItems} onClick={onClaimReview}>
              Start Review
            </Button>
          )}
          {onAbort && (
            <span title={destructiveDisabledReason || 'Abort processing for selected documents'}>
              <Button
                iconName="status-stopped"
                variant="normal"
                disabled={!!destructiveDisabledReason || !hasAbortableItems}
                onClick={onAbort}
                ariaLabel="Abort"
              />
            </span>
          )}
          {onReprocess && (
            <span title={destructiveDisabledReason || 'Reprocess selected documents'}>
              <Button
                iconName="redo"
                variant="normal"
                disabled={!!destructiveDisabledReason || !hasSelectedItems}
                onClick={onReprocess}
                ariaLabel="Reprocess"
              />
            </span>
          )}
          {onDelete && (
            <span title={destructiveDisabledReason || 'Delete selected documents'}>
              <Button
                iconName="remove"
                variant="normal"
                disabled={!!destructiveDisabledReason || !hasSelectedItems}
                onClick={onDelete}
                ariaLabel="Delete"
              />
            </span>
          )}
          {onReleaseReview && (
            <span title="Release review lock on selected documents">
              <Button
                iconName="unlocked"
                variant="normal"
                disabled={!hasReleasableItems}
                onClick={onReleaseReview}
                ariaLabel="Release Review"
              />
            </span>
          )}
          <CircuitBreakerBadge />
        </SpaceBetween>
      }
      {...props}
    />
  );
};
