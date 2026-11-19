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
import { defineMessages, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { isBaselineAvailable, type ExportScope } from '../document-panel/document-export';

import { TableHeader } from '../common/table';
import { DOCUMENTS_PATH, documentEditHref, testRunResultsHref } from '../../routes/constants';
import type { DocumentView } from '../../contexts/documents';
import { useFormatters } from '../../i18n/format';
import { renderHitlStatus } from '../common/hitl-status-renderer';
import { isInReviewQueue, isQueueManaged } from '../common/review-status';
import { renderObjectStatus } from '../common/object-status-renderer';
import { MALWARE_REFUSED_STATUSES, MALWARE_SCANNING_STATUS } from '../common/malware-scan';
import CircuitBreakerBadge from './CircuitBreakerBadge';
import AddToTestSetButton from './AddToTestSetButton';
import { formatConfigVersionLink } from '../test-studio/utils/configVersionUtils';
import type { ConfigVersion } from '../test-studio/utils/configVersionUtils';
import { ABORTABLE_STATUSES } from '../common/document-statuses';

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
  missingRequiredFieldCount?: number | null;
  processingIssueCount?: number | null;
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
  text: TypedMessageDescriptor<NoMessageValues>;
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
  /** The Review menu (built by buildReviewMenuItems) and its handler; absent = no menu. */
  reviewMenu?: { items: ButtonDropdownProps.Items; visible: boolean; disabledReason?: string } | null;
  onReviewAction?: ((id: string) => void) | null;
  /** Opens the add-to-test-set dialog. Null hides the action (it is offered to Admins only). */
  onAddToTestSet?: (() => void) | null;
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

/** The column headers, keyed by column id; the Preferences dialog names the columns with the same messages. */
const columnMessages = defineMessages<{
  testRunId: NoMessageValues;
  objectKey: NoMessageValues;
  objectStatus: NoMessageValues;
  configVersion: NoMessageValues;
  initialEventTime: NoMessageValues;
  completionTime: NoMessageValues;
  duration: NoMessageValues;
  evaluationStatus: NoMessageValues;
  confidenceAlertCount: NoMessageValues;
  missingRequiredFieldCount: NoMessageValues;
  processingIssueCount: NoMessageValues;
  hitlStatus: NoMessageValues;
  hitlReviewOwner: NoMessageValues;
  hitlReviewedBy: NoMessageValues;
}>({
  testRunId: {
    id: 'documents.table.column.testRunId',
    defaultMessage: 'Test Run',
    description: 'Column header of the document list: the Test Studio run that submitted the document.',
  },
  objectKey: {
    id: 'documents.table.column.objectKey',
    defaultMessage: 'Document ID',
    description: 'Column header of the document list: the document key.',
  },
  objectStatus: {
    id: 'documents.table.column.objectStatus',
    defaultMessage: 'Status',
    description: 'Column header of the document list: the processing status.',
  },
  configVersion: {
    id: 'documents.table.column.configVersion',
    defaultMessage: 'Config Profile',
    description: 'Column header of the document list: the configuration profile the document was processed with.',
  },
  initialEventTime: {
    id: 'documents.table.column.initialEventTime',
    defaultMessage: 'Submitted',
    description: 'Column header of the document list: when the document was submitted.',
  },
  completionTime: {
    id: 'documents.table.column.completionTime',
    defaultMessage: 'Completed',
    description: 'Column header of the document list: when processing finished.',
  },
  duration: {
    id: 'documents.table.column.duration',
    defaultMessage: 'Duration',
    description: 'Column header of the document list: how long processing took.',
  },
  evaluationStatus: {
    id: 'documents.table.column.evaluationStatus',
    defaultMessage: 'Evaluation',
    description: 'Column header of the document list: the evaluation status against a baseline.',
  },
  confidenceAlertCount: {
    id: 'documents.table.column.confidenceAlertCount',
    defaultMessage: 'Confidence Alerts',
    description: 'Column header of the document list: the number of low-confidence fields.',
  },
  missingRequiredFieldCount: {
    id: 'documents.table.column.missingRequiredFieldCount',
    defaultMessage: 'Missing Required Fields',
    description: 'Column header of the document list: the number of required fields with no value.',
  },
  processingIssueCount: {
    id: 'documents.table.column.processingIssueCount',
    defaultMessage: 'Processing Issues',
    description: 'Column header of the document list: the number of issues raised while processing.',
  },
  hitlStatus: {
    id: 'documents.table.column.hitlStatus',
    defaultMessage: 'Review Status',
    description: 'Column header of the document list: the human review status.',
  },
  hitlReviewOwner: {
    id: 'documents.table.column.hitlReviewOwner',
    defaultMessage: 'Review Owner',
    description: 'Column header of the document list: who holds the review.',
  },
  hitlReviewedBy: {
    id: 'documents.table.column.hitlReviewedBy',
    defaultMessage: 'Review Completed By',
    description: 'Column header of the document list: who completed the review.',
  },
});

const messages = defineMessages<{
  notApplicable: NoMessageValues;
  downloadInProgress: NoMessageValues;
  downloadSelectFirst: NoMessageValues;
  downloadListGroup: NoMessageValues;
  downloadExcel: { readonly count: number };
  downloadSelectedGroup: { readonly count: number };
  downloadAll: NoMessageValues;
  downloadPredictions: NoMessageValues;
  downloadBaselines: NoMessageValues;
  downloadNoBaseline: NoMessageValues;
  downloadLabel: NoMessageValues;
  selectionItem: { readonly name: string };
  selectionAll: NoMessageValues;
  selectionGroup: NoMessageValues;
  pageSizeOption: { readonly count: number };
  columnsGroup: NoMessageValues;
  preferencesTitle: NoMessageValues;
  preferencesConfirm: NoMessageValues;
  preferencesCancel: NoMessageValues;
  pageSize: NoMessageValues;
  wrapLines: NoMessageValues;
  wrapLinesDescription: NoMessageValues;
  visibleColumns: NoMessageValues;
  loadButton: { readonly period: string };
  sourceLabel: NoMessageValues;
  sourceProduction: NoMessageValues;
  sourceTestStudio: NoMessageValues;
  reprocessRefused: { readonly count: number };
  reprocessScanning: { readonly count: number };
  refreshTitle: NoMessageValues;
  refreshLabel: NoMessageValues;
  reviewLabel: NoMessageValues;
  abortTitle: NoMessageValues;
  abortLabel: NoMessageValues;
  reprocessTitle: NoMessageValues;
  reprocessLabel: NoMessageValues;
  deleteTitle: NoMessageValues;
  deleteLabel: NoMessageValues;
}>({
  notApplicable: {
    id: 'documents.table.notApplicable',
    defaultMessage: 'N/A',
    description: 'Evaluation cell of the document list for a document that was not evaluated.',
  },
  downloadInProgress: {
    id: 'documents.table.download.inProgress',
    defaultMessage: 'A download is already in progress',
    description: 'Why the ZIP items of the Download menu are disabled while an export runs.',
  },
  downloadSelectFirst: {
    id: 'documents.table.download.selectFirst',
    defaultMessage: 'Select one or more documents in the table',
    description: 'Why the ZIP items of the Download menu are disabled when no document is selected.',
  },
  downloadListGroup: {
    id: 'documents.table.download.listGroup',
    defaultMessage: 'Document list',
    description: 'Group label in the Download menu above the export of the whole list.',
  },
  downloadExcel: {
    id: 'documents.table.download.excel',
    defaultMessage: 'Table as Excel ({count, plural, one {# row} other {# rows}})',
    description: 'Download menu item exporting the filtered list as an Excel file. {count} is the number of rows.',
  },
  downloadSelectedGroup: {
    id: 'documents.table.download.selectedGroup',
    defaultMessage: 'Selected documents ({count, number})',
    description: 'Group label in the Download menu above the ZIP exports. {count} is the number of selected documents.',
  },
  downloadAll: {
    id: 'documents.table.download.all',
    defaultMessage: 'All data (ZIP)',
    description: 'Download menu item: every artifact of the selected documents as a ZIP.',
  },
  downloadPredictions: {
    id: 'documents.table.download.predictions',
    defaultMessage: 'Predictions (ZIP)',
    description: 'Download menu item: the predictions of the selected documents as a ZIP.',
  },
  downloadBaselines: {
    id: 'documents.table.download.baselines',
    defaultMessage: 'Baselines (ZIP)',
    description: 'Download menu item: the evaluation baselines of the selected documents as a ZIP.',
  },
  downloadNoBaseline: {
    id: 'documents.table.download.noBaseline',
    defaultMessage: 'No selected document has an evaluation baseline',
    description: 'Why the Baselines item of the Download menu is disabled.',
  },
  downloadLabel: {
    id: 'documents.table.download.label',
    defaultMessage: 'Download',
    description: 'Text and screen-reader label of the Download menu button of the document list.',
  },
  selectionItem: {
    id: 'documents.table.selection.item',
    defaultMessage: 'select {name}',
    description: 'Screen-reader label of a row checkbox of the document list. {name} is the document key.',
  },
  selectionAll: {
    id: 'documents.table.selection.all',
    defaultMessage: 'select all',
    description: 'Screen-reader label of the select-all checkbox of the document list.',
  },
  selectionGroup: {
    id: 'documents.table.selection.group',
    defaultMessage: 'Document selection',
    description: 'Screen-reader name of the group of row checkboxes of the document list.',
  },
  pageSizeOption: {
    id: 'documents.table.preferences.pageSizeOption',
    defaultMessage: '{count, plural, one {# Document} other {# Documents}}',
    description: 'A page-size choice in the Preferences dialog of the document list. {count} is the number of rows per page.',
  },
  columnsGroup: {
    id: 'documents.table.preferences.columnsGroup',
    defaultMessage: 'Document list properties',
    description: 'Group label above the column checkboxes in the Preferences dialog of the document list.',
  },
  preferencesTitle: {
    id: 'documents.table.preferences.title',
    defaultMessage: 'Preferences',
    description: 'Title of the Preferences dialog of the document list.',
  },
  preferencesConfirm: {
    id: 'documents.table.preferences.confirm',
    defaultMessage: 'Confirm',
    description: 'Button that saves the Preferences dialog of the document list.',
  },
  preferencesCancel: {
    id: 'documents.table.preferences.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the Preferences dialog of the document list without saving.',
  },
  pageSize: {
    id: 'documents.table.preferences.pageSize',
    defaultMessage: 'Page size',
    description: 'Heading of the page-size choices in the Preferences dialog of the document list.',
  },
  wrapLines: {
    id: 'documents.table.preferences.wrapLines',
    defaultMessage: 'Wrap lines',
    description: 'Checkbox in the Preferences dialog of the document list that wraps long cell text.',
  },
  wrapLinesDescription: {
    id: 'documents.table.preferences.wrapLinesDescription',
    defaultMessage: 'Check to see all the text and wrap the lines',
    description: 'Help text under the Wrap lines checkbox in the Preferences dialog of the document list.',
  },
  visibleColumns: {
    id: 'documents.table.preferences.visibleColumns',
    defaultMessage: 'Select visible columns',
    description: 'Heading of the column checkboxes in the Preferences dialog of the document list.',
  },
  loadButton: {
    id: 'documents.table.load.button',
    defaultMessage: 'Load: {period}',
    description: 'Menu button of the document list that picks how far back to load. {period} is the chosen period or date range.',
  },
  sourceLabel: {
    id: 'documents.table.source.label',
    defaultMessage: 'Document source',
    description: 'Screen-reader label of the control that switches the document list between production and Test Studio documents.',
  },
  sourceProduction: {
    id: 'documents.table.source.production',
    defaultMessage: 'Production',
    description: 'Document source option: documents submitted for processing (uploads, API, CLI, extensions).',
  },
  sourceTestStudio: {
    id: 'documents.table.source.testStudio',
    defaultMessage: 'Test Studio',
    description: 'Document source option: documents submitted by Test Studio runs.',
  },
  reprocessRefused: {
    id: 'documents.table.reprocess.refused',
    defaultMessage:
      '{count, plural, one {# selected document was} other {# selected documents were}} refused by malware scanning and cannot be reprocessed. Delete {count, plural, one {it} other {them}}, or re-upload an unprotected copy of a file GuardDuty could not read.',
    description:
      'Tooltip of the disabled Reprocess button when the selection holds documents refused by malware scanning. {count} is how many.',
  },
  reprocessScanning: {
    id: 'documents.table.reprocess.scanning',
    defaultMessage:
      '{count, plural, one {# selected document is} other {# selected documents are}} still being scanned for malware. Reprocess is available once the scan verdict arrives.',
    description: 'Tooltip of the disabled Reprocess button when the selection holds documents still being scanned. {count} is how many.',
  },
  refreshTitle: {
    id: 'documents.table.refresh.title',
    defaultMessage: 'Refresh document list',
    description: 'Tooltip of the refresh button of the document list.',
  },
  refreshLabel: {
    id: 'documents.table.refresh.ariaLabel',
    defaultMessage: 'Refresh',
    description: 'Screen-reader label of the refresh button of the document list.',
  },
  reviewLabel: {
    id: 'documents.table.review.label',
    defaultMessage: 'Review',
    description: 'Text and screen-reader label of the Review menu button of the document list.',
  },
  abortTitle: {
    id: 'documents.table.abort.title',
    defaultMessage: 'Abort processing for selected documents',
    description: 'Tooltip of the Abort button of the document list.',
  },
  abortLabel: {
    id: 'documents.table.abort.label',
    defaultMessage: 'Abort',
    description: 'Screen-reader label of the Abort button of the document list.',
  },
  reprocessTitle: {
    id: 'documents.table.reprocess.title',
    defaultMessage: 'Reprocess selected documents',
    description: 'Tooltip of the Reprocess button of the document list.',
  },
  reprocessLabel: {
    id: 'documents.table.reprocess.label',
    defaultMessage: 'Reprocess',
    description: 'Screen-reader label of the Reprocess button of the document list.',
  },
  deleteTitle: {
    id: 'documents.table.delete.title',
    defaultMessage: 'Delete selected documents',
    description: 'Tooltip of the Delete button of the document list.',
  },
  deleteLabel: {
    id: 'documents.table.delete.label',
    defaultMessage: 'Delete',
    description: 'Screen-reader label of the Delete button of the document list.',
  },
});

/** The Load menu's periods; each is a message so the menu reads in the UI's language. */
const loadMessages = defineMessages<{
  latest: NoMessageValues;
  hours2: NoMessageValues;
  hours4: NoMessageValues;
  hours8: NoMessageValues;
  day1: NoMessageValues;
  days2: NoMessageValues;
  week1: NoMessageValues;
  weeks2: NoMessageValues;
  days30: NoMessageValues;
  customRange: NoMessageValues;
}>({
  latest: {
    id: 'documents.table.load.latest',
    defaultMessage: 'Latest',
    description: 'Load menu option of the document list: the newest documents, with no date bounds.',
  },
  hours2: {
    id: 'documents.table.load.hours2',
    defaultMessage: '2 hrs',
    description: 'Load menu option of the document list: the last 2 hours.',
  },
  hours4: {
    id: 'documents.table.load.hours4',
    defaultMessage: '4 hrs',
    description: 'Load menu option of the document list: the last 4 hours.',
  },
  hours8: {
    id: 'documents.table.load.hours8',
    defaultMessage: '8 hrs',
    description: 'Load menu option of the document list: the last 8 hours.',
  },
  day1: {
    id: 'documents.table.load.day1',
    defaultMessage: '1 day',
    description: 'Load menu option of the document list: the last day.',
  },
  days2: {
    id: 'documents.table.load.days2',
    defaultMessage: '2 days',
    description: 'Load menu option of the document list: the last 2 days.',
  },
  week1: {
    id: 'documents.table.load.week1',
    defaultMessage: '1 week',
    description: 'Load menu option of the document list: the last week.',
  },
  weeks2: {
    id: 'documents.table.load.weeks2',
    defaultMessage: '2 weeks',
    description: 'Load menu option of the document list: the last 2 weeks.',
  },
  days30: {
    id: 'documents.table.load.days30',
    defaultMessage: '30 days',
    description: 'Load menu option of the document list: the last 30 days.',
  },
  customRange: {
    id: 'documents.table.load.customRange',
    defaultMessage: 'Custom range...',
    description: 'Load menu option of the document list that opens the date range dialog.',
  },
});

/** The page title of the document list by document source; the header shows `title` when the page passes none. */
export const listTitleMessages = defineMessages<{ title: NoMessageValues; titleTest: NoMessageValues }>({
  title: {
    id: 'documents.list.title',
    defaultMessage: 'Documents',
    description: 'Heading of the document list page in the Production view.',
  },
  titleTest: {
    id: 'documents.list.titleTest',
    defaultMessage: 'Test Studio Documents',
    description: 'Heading of the document list page in the Test Studio view, which lists the documents submitted by test runs.',
  },
});

/** The text in the UI's language; without `intl`, the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/**
 * Two download actions live in one menu, split into labeled groups because they
 * differ in scope: the Excel export covers every filtered row, while the ZIP
 * exports cover only the selected documents. The Excel item stays enabled while a
 * ZIP export runs — only the ZIP scopes are held.
 */
export const buildDownloadMenuItems = (
  intl: IntlShape,
  selectedItems: readonly MappedDocument[],
  filteredCount: number,
  includeZipScopes = true,
  isDownloadInProgress = false,
): ButtonDropdownProps.ItemOrGroup[] => {
  const selectedCount = selectedItems.length;
  const noSelection = selectedCount === 0;
  const blockedReason = (() => {
    if (isDownloadInProgress) return intl.formatMessage(messages.downloadInProgress);
    if (noSelection) return intl.formatMessage(messages.downloadSelectFirst);
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
    text: intl.formatMessage(messages.downloadListGroup),
    items: [
      {
        id: 'excel',
        text: intl.formatMessage(messages.downloadExcel, { count: filteredCount }),
        iconName: 'download',
      },
    ],
  };
  if (!includeZipScopes) return [listGroup];

  return [
    listGroup,
    {
      text: intl.formatMessage(messages.downloadSelectedGroup, { count: selectedCount }),
      items: [
        zipItem('all', intl.formatMessage(messages.downloadAll)),
        zipItem('predictions', intl.formatMessage(messages.downloadPredictions)),
        zipItem(
          'baselines',
          intl.formatMessage(messages.downloadBaselines),
          !noSelection && !selectedItems.some(isBaselineAvailable) ? intl.formatMessage(messages.downloadNoBaseline) : undefined,
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

const testRunColumn = (intl?: IntlShape): TableProps.ColumnDefinition<MappedDocument> => ({
  id: TEST_RUN_COLUMN_ID,
  header: labelOf(columnMessages.testRunId, intl),
  cell: (item) => {
    const testRunId = testRunIdFromObjectKey(item.objectKey);
    if (!testRunId) return '-';
    return <Link href={testRunResultsHref(testRunId)}>{testRunId}</Link>;
  },
  sortingComparator: (a, b) => (testRunIdFromObjectKey(a.objectKey) ?? '').localeCompare(testRunIdFromObjectKey(b.objectKey) ?? ''),
  width: 260,
});

/**
 * A stored timestamp as a date and time in the UI locale and the browser's time zone; a
 * column's `cell` cannot call a hook, so this is a component. An empty value (no timestamp)
 * is an invalid date too, so it stays empty, and any other value `Date` cannot parse is shown
 * as stored. Display only: the column sorts, and the list's search matches, on the stored value.
 */
const DateTimeCell = ({ value }: { value: string }): React.JSX.Element => {
  const { formatDateTime } = useFormatters();
  const date = new Date(value);
  return <>{Number.isNaN(date.getTime()) ? value : formatDateTime(date)}</>;
};

/**
 * `canWorkReviewQueues` links a queued document's In Review status to its queue item,
 * the same destination as the document page's Review button. Without `intl` the
 * headers and cell labels are the English source text.
 */
export const COLUMN_DEFINITIONS_MAIN = (
  versions: ConfigVersion[] = [],
  documentView: DocumentView = 'PRODUCTION',
  canWorkReviewQueues = false,
  intl?: IntlShape,
): TableProps.ColumnDefinition<MappedDocument>[] => [
  {
    id: KEY_COLUMN_ID,
    header: labelOf(columnMessages.objectKey, intl),
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
  ...(documentView === 'TEST' ? [testRunColumn(intl)] : []),
  {
    id: 'objectStatus',
    header: labelOf(columnMessages.objectStatus, intl),
    cell: (item) => renderObjectStatus(item.objectStatus, intl),
    sortingField: 'objectStatus',
    width: 150,
  },
  {
    id: 'configVersion',
    header: labelOf(columnMessages.configVersion, intl),
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
    header: labelOf(columnMessages.initialEventTime, intl),
    cell: (item) => <DateTimeCell value={item.initialEventTime} />,
    sortingField: 'initialEventTime',
    width: 225,
  },
  {
    id: 'completionTime',
    header: labelOf(columnMessages.completionTime, intl),
    cell: (item) => <DateTimeCell value={item.completionTime} />,
    sortingField: 'completionTime',
    width: 225,
  },
  {
    id: 'duration',
    header: labelOf(columnMessages.duration, intl),
    cell: (item) => item.duration,
    sortingField: 'duration',
    width: 150,
  },
  {
    id: 'evaluationStatus',
    header: labelOf(columnMessages.evaluationStatus, intl),
    cell: (item) => item.evaluationStatus || labelOf(messages.notApplicable, intl),
    sortingField: 'evaluationStatus',
    width: 150,
  },
  {
    id: 'confidenceAlertCount',
    header: labelOf(columnMessages.confidenceAlertCount, intl),
    cell: (item) => item.confidenceAlertCount || 0,
    sortingField: 'confidenceAlertCount',
    width: 150,
  },
  {
    id: 'missingRequiredFieldCount',
    header: labelOf(columnMessages.missingRequiredFieldCount, intl),
    // Null when this row does not carry the count (written before the count
    // existed, or served while the list index backfills after an upgrade): a
    // dash, not a 0 the row cannot substantiate.
    cell: (item) => (item.missingRequiredFieldCount == null ? '-' : item.missingRequiredFieldCount),
    sortingField: 'missingRequiredFieldCount',
    width: 180,
  },
  {
    id: 'processingIssueCount',
    header: labelOf(columnMessages.processingIssueCount, intl),
    // A dash when the row does not carry the count, for the same reason as the
    // column above; a green 0 would claim the document was checked and clean.
    cell: (item) => {
      const count = item.processingIssueCount;
      if (count == null) return '-';
      return count === 0 ? <StatusIndicator type="success">0</StatusIndicator> : <StatusIndicator type="warning">{count}</StatusIndicator>;
    },
    sortingField: 'processingIssueCount',
    width: 150,
  },
  {
    id: 'hitlStatus',
    header: labelOf(columnMessages.hitlStatus, intl),
    cell: (item) => renderHitlStatus(item, canWorkReviewQueues ? documentEditHref(item.objectKey, { from: 'document' }) : undefined, intl),
    sortingField: 'hitlStatus',
    width: 150,
  },
  {
    id: 'hitlReviewOwner',
    header: labelOf(columnMessages.hitlReviewOwner, intl),
    cell: (item) => item.hitlReviewOwnerEmail || item.hitlReviewOwner || '-',
    sortingField: 'hitlReviewOwner',
    width: 180,
  },
  {
    id: 'hitlReviewedBy',
    header: labelOf(columnMessages.hitlReviewedBy, intl),
    cell: (item) => item.hitlReviewedByEmail || item.hitlReviewedBy || '-',
    sortingField: 'hitlReviewedBy',
    width: 180,
  },
];

export const DEFAULT_SORT_COLUMN = { sortingField: 'initialEventTime' };

/** The table's selection aria labels in the UI's language. */
export const selectionLabels = (intl: IntlShape): TableProps.AriaLabels<MappedDocument> => ({
  itemSelectionLabel: (_data, row) => intl.formatMessage(messages.selectionItem, { name: row.objectKey }),
  allItemsSelectionLabel: () => intl.formatMessage(messages.selectionAll),
  selectionGroupLabel: intl.formatMessage(messages.selectionGroup),
});

const PAGE_SIZES = [10, 30, 50];

/** The Preferences dialog's page-size choices in the UI's language. */
export const buildPageSizeOptions = (intl: IntlShape): CollectionPreferencesProps.PageSizeOption[] =>
  PAGE_SIZES.map((value) => ({ value, label: intl.formatMessage(messages.pageSizeOption, { count: value }) }));

/** The columns the Preferences dialog offers, in its order; Document ID cannot be hidden. */
const VISIBLE_CONTENT_COLUMNS = [
  'objectKey',
  'objectStatus',
  'initialEventTime',
  'completionTime',
  'duration',
  'configVersion',
  'evaluationStatus',
  'confidenceAlertCount',
  'missingRequiredFieldCount',
  'processingIssueCount',
  'hitlStatus',
  'hitlReviewOwner',
  'hitlReviewedBy',
] as const;

const buildVisibleContentOptions = (intl: IntlShape): CollectionPreferencesProps.VisibleContentOptionsGroup[] => [
  {
    label: intl.formatMessage(messages.columnsGroup),
    options: VISIBLE_CONTENT_COLUMNS.map((id) => ({
      id,
      label: intl.formatMessage(columnMessages[id]),
      ...(id === KEY_COLUMN_ID ? { editable: false } : {}),
    })),
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
  pageSize: PAGE_SIZES[0],
  visibleContent: VISIBLE_CONTENT,
  wrapLines: false,
};

export const DocumentsPreferences = ({
  preferences,
  setPreferences,
  disabled,
  pageSizeOptions,
  visibleContentOptions,
}: DocumentsPreferencesProps): React.JSX.Element => {
  const intl = useIntl();
  return (
    <CollectionPreferences
      title={intl.formatMessage(messages.preferencesTitle)}
      confirmLabel={intl.formatMessage(messages.preferencesConfirm)}
      cancelLabel={intl.formatMessage(messages.preferencesCancel)}
      disabled={disabled}
      preferences={preferences}
      onConfirm={({ detail }) => setPreferences(detail)}
      pageSizePreference={{
        title: intl.formatMessage(messages.pageSize),
        options: pageSizeOptions ?? buildPageSizeOptions(intl),
      }}
      wrapLinesPreference={{
        label: intl.formatMessage(messages.wrapLines),
        description: intl.formatMessage(messages.wrapLinesDescription),
      }}
      visibleContentPreference={{
        title: intl.formatMessage(messages.visibleColumns),
        options: visibleContentOptions ?? buildVisibleContentOptions(intl),
      }}
    />
  );
};

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
  'load-latest': { count: LATEST_PERIODS, text: loadMessages.latest },
  'refresh-2h': { count: 0.5, text: loadMessages.hours2 },
  'refresh-4h': { count: 1, text: loadMessages.hours4 },
  'refresh-8h': { count: DOCUMENT_LIST_SHARDS_PER_DAY / 3, text: loadMessages.hours8 },
  'refresh-1d': { count: DOCUMENT_LIST_SHARDS_PER_DAY, text: loadMessages.day1 },
  'refresh-2d': { count: 2 * DOCUMENT_LIST_SHARDS_PER_DAY, text: loadMessages.days2 },
  'refresh-1w': { count: 7 * DOCUMENT_LIST_SHARDS_PER_DAY, text: loadMessages.week1 },
  'refresh-2w': { count: 14 * DOCUMENT_LIST_SHARDS_PER_DAY, text: loadMessages.weeks2 },
  'refresh-1m': { count: 30 * DOCUMENT_LIST_SHARDS_PER_DAY, text: loadMessages.days30 },
  'custom-range': { count: -1, text: loadMessages.customRange },
};
/** The Load menu's items in the UI's language, each keeping its `count` for the display-text lookup. */
const timePeriodDropdownItems = (intl: IntlShape) =>
  Object.keys(TIME_PERIOD_DROPDOWN_CONFIG).map((k) => ({
    id: k,
    count: TIME_PERIOD_DROPDOWN_CONFIG[k].count,
    text: intl.formatMessage(TIME_PERIOD_DROPDOWN_CONFIG[k].text),
  }));

// local storage key to persist the last periods to load
export const PERIODS_TO_LOAD_STORAGE_KEY = 'periodsToLoad';

// local storage key to persist custom date range
export const CUSTOM_DATE_RANGE_STORAGE_KEY = 'customDateRange';

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
}): React.JSX.Element => {
  const intl = useIntl();
  return (
    <SegmentedControl
      selectedId={documentView}
      onChange={({ detail }) => setDocumentView(detail.selectedId as DocumentView)}
      label={intl.formatMessage(messages.sourceLabel)}
      options={[
        // "Production" rather than "Uploads": API, CLI and extension submissions land
        // in this partition too, not just documents uploaded through the web UI.
        { id: 'PRODUCTION', text: intl.formatMessage(messages.sourceProduction), disabled },
        { id: 'TEST', text: intl.formatMessage(messages.sourceTestStudio), disabled },
      ]}
    />
  );
};

/**
 * Whether Start Review can claim this document: a review was raised on it, it has
 * not finished, and nobody owns it. The server refuses a claim where no review was
 * raised (`NO_REVIEW_PENDING`) or the review has finished (`REVIEW_FINISHED`). A
 * queued document is claimed from its queue item, so it never counts here.
 */
export const isClaimableForReview = (item: MappedDocument): boolean =>
  Boolean(item.hitlTriggered && !item.hitlCompleted && !item.hitlReviewOwner && !isQueueManaged(item.hitlStatus));

/**
 * Whether Release Review can release this document: somebody has claimed it and
 * its review has not finished. The server refuses a release nobody claimed
 * (`NOT_CLAIMED`) or of a finished review (`REVIEW_FINISHED`). A queued document
 * is released from its queue item, so it never counts here.
 */
export const isReleasableForReview = (item: MappedDocument): boolean =>
  Boolean(item.hitlReviewOwner && !item.hitlCompleted && !isQueueManaged(item.hitlStatus));

/**
 * Whether Skip All Reviews (N selected) counts this document: its review is in a
 * production queue, or a legacy review is pending that nobody else holds (an Admin
 * may skip any). An estimate: the server decides each document (queue claimant, Test
 * Studio provenance and scope are not on the row).
 */
export const isSkippableForReview = (item: MappedDocument, currentUsername?: string, isAdmin = false): boolean => {
  if (isInReviewQueue(item.hitlStatus)) return true;
  if (!item.hitlTriggered || item.hitlCompleted || isQueueManaged(item.hitlStatus)) return false;
  return !item.hitlReviewOwner || isAdmin || item.hitlReviewOwner === currentUsername;
};

/** Whether Send to review counts this document: finished, and in no review. */
export const isSendableToReview = (item: MappedDocument): boolean =>
  item.objectStatus === 'COMPLETED' &&
  !isInReviewQueue(item.hitlStatus) &&
  !(item.hitlTriggered && !item.hitlCompleted && !isQueueManaged(item.hitlStatus));

export const DocumentsCommonHeader = ({
  resourceName,
  selectedItems = [],
  onDelete,
  onReprocess,
  onAbort,
  onClaimReview,
  onReleaseReview,
  onAddToTestSet,
  onDownloadSelected,
  isDownloadInProgress,
  destructiveDisabledReason,
  reviewMenu,
  onReviewAction,
  currentUsername: _currentUsername,
  ...props
}: DocumentsCommonHeaderProps): React.JSX.Element => {
  const intl = useIntl();
  const timePeriodItems = timePeriodDropdownItems(intl);
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
    return timePeriodItems.filter((i) => i.count === props.periodsToLoad)[0]?.text || '';
  };

  const periodText = getDisplayText();

  const hasSelectedItems = selectedItems.length > 0;
  // Check if any selected items can be aborted
  const hasAbortableItems = selectedItems.some((item) => ABORTABLE_STATUSES.includes(item.objectStatus));
  // Reprocess re-queues a document directly, bypassing the malware-scanning
  // ingest gate — the refused file is still in the input bucket, so reprocessing
  // a quarantined document would push it into OCR and Bedrock. The resolver
  // refuses it server-side; disabling the button here is so a reviewer finds out
  // before clicking rather than from an error. ANY refused document in the
  // selection disables it, because reprocess acts on the whole selection.
  const refusedSelection = selectedItems.filter((item) => MALWARE_REFUSED_STATUSES.includes((item.objectStatus || '').toUpperCase()));
  // Held at the gate, awaiting a verdict. Blocked for a different reason than a
  // refusal and so worded differently: this one clears by itself in a minute or
  // two, whereas a refusal needs the file deleted or replaced. Reprocessing one
  // of these would run it through OCR and Bedrock with no verdict.
  const scanningSelection = selectedItems.filter((item) => (item.objectStatus || '').toUpperCase() === MALWARE_SCANNING_STATUS);
  const reprocessDisabledReason =
    refusedSelection.length > 0
      ? intl.formatMessage(messages.reprocessRefused, { count: refusedSelection.length })
      : scanningSelection.length > 0
        ? intl.formatMessage(messages.reprocessScanning, { count: scanningSelection.length })
        : null;

  const downloadMenuItems = buildDownloadMenuItems(
    intl,
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
      title={resourceName ?? intl.formatMessage(listTitleMessages.title)}
      actionButtons={
        <SpaceBetween size="xxs" direction="horizontal">
          <ButtonDropdown loading={props.loading} onItemClick={onPeriodToLoadChange} items={timePeriodItems}>
            {intl.formatMessage(messages.loadButton, { period: periodText })}
          </ButtonDropdown>
          <span title={intl.formatMessage(messages.refreshTitle)}>
            <Button
              iconName="refresh"
              variant="normal"
              loading={props.loading}
              onClick={() => props.setIsLoading?.(true)}
              ariaLabel={intl.formatMessage(messages.refreshLabel)}
            />
          </span>
          <ButtonDropdown
            items={downloadMenuItems}
            onItemClick={onDownloadItemClick}
            loading={props.loading}
            expandToViewport
            ariaLabel={intl.formatMessage(messages.downloadLabel)}
          >
            {intl.formatMessage(messages.downloadLabel)}
          </ButtonDropdown>
          {onAddToTestSet && <AddToTestSetButton selectedItems={selectedItems} onClick={onAddToTestSet} />}
          {reviewMenu?.visible && (
            <ButtonDropdown
              items={reviewMenu.items}
              disabled={!!reviewMenu.disabledReason}
              disabledReason={reviewMenu.disabledReason}
              onItemClick={({ detail }) => {
                if (detail.id === 'claim') onClaimReview?.();
                else if (detail.id === 'release') onReleaseReview?.();
                else onReviewAction?.(detail.id);
              }}
              expandToViewport
              ariaLabel={intl.formatMessage(messages.reviewLabel)}
            >
              {intl.formatMessage(messages.reviewLabel)}
            </ButtonDropdown>
          )}
          {onAbort && (
            <span title={destructiveDisabledReason || intl.formatMessage(messages.abortTitle)}>
              <Button
                iconName="status-stopped"
                variant="normal"
                disabled={!!destructiveDisabledReason || !hasAbortableItems}
                onClick={onAbort}
                ariaLabel={intl.formatMessage(messages.abortLabel)}
              />
            </span>
          )}
          {onReprocess && (
            <span title={destructiveDisabledReason || reprocessDisabledReason || intl.formatMessage(messages.reprocessTitle)}>
              <Button
                iconName="redo"
                variant="normal"
                disabled={!!destructiveDisabledReason || !!reprocessDisabledReason || !hasSelectedItems}
                onClick={onReprocess}
                ariaLabel={intl.formatMessage(messages.reprocessLabel)}
              />
            </span>
          )}
          {onDelete && (
            <span title={destructiveDisabledReason || intl.formatMessage(messages.deleteTitle)}>
              <Button
                iconName="remove"
                variant="normal"
                disabled={!!destructiveDisabledReason || !hasSelectedItems}
                onClick={onDelete}
                ariaLabel={intl.formatMessage(messages.deleteLabel)}
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
