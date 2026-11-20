// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  Container,
  ExpandableSection,
  Header,
  Modal,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { listDocumentVersions, getDocumentVersion, compareDocumentVersions, deleteDocumentVersion } from '../../graphql/generated';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { useFormatters } from '../../i18n/format';
import { formatStoredTime } from './stored-time';

const logger = new ConsoleLogger('DocumentVersionsPanel');
const client = generateClient();

interface DocumentVersion {
  RunId: string;
  ObjectKey?: string;
  CompletionTime?: string;
  ConfigVersion?: string;
  PageCount?: number;
  WorkflowExecutionArn?: string;
}

interface VersionChange {
  path: string;
  type: string;
  a?: unknown;
  b?: unknown;
}

interface SectionDiff {
  section: string;
  status: string;
  changes: VersionChange[];
}

interface CompareResult {
  objectKey: string;
  runIdA: string;
  runIdB: string;
  configVersionA?: string;
  configVersionB?: string;
  completionTimeA?: string;
  completionTimeB?: string;
  identical: boolean;
  sections: SectionDiff[];
}

/**
 * The error shown above the version table. It is held as a message and formatted at render, so
 * it follows a language switch. An API failure keeps the action phrase and what the call threw,
 * for `describeApiError` to word.
 */
type PanelError =
  | { message: TypedMessageDescriptor<NoMessageValues> }
  | { action: TypedMessageDescriptor<NoMessageValues>; cause: unknown };

interface DocumentVersionsPanelProps {
  objectKey: string;
  /** RunId currently being viewed on the page, or null when viewing current. */
  viewingRunId?: string | null;
  /**
   * Select a version to view on the page. Passes the full run detail
   * (Sections/Pages snapshot + manifest Files with per-object VersionId) so the
   * page can render that run's structure and fetch its exact bytes, or null to
   * return to the current version.
   */
  onViewVersion?: (runId: string | null, detail: DocumentVersionDetail | null) => void;
  /**
   * When the document's current run finished. A run is recorded as a version when
   * it completes, so a change here means the list on screen is out of date: a
   * document reprocessed while its page was open finished with the old list shown.
   */
  completionTime?: string | null;
}

/** Full run detail returned by getDocumentVersion (Sections/Pages + Files). */
export interface DocumentVersionDetail {
  RunId?: string;
  Sections?: Record<string, unknown>[] | null;
  Pages?: Record<string, unknown>[] | null;
  Files?: { Key?: string | null; VersionId?: string | null }[] | null;
  SummaryReportUri?: string | null;
  EvaluationReportUri?: string | null;
  Metering?: string | null;
}

const messages = defineMessages<{
  title: NoMessageValues;
  description: NoMessageValues;
  refresh: NoMessageValues;
  compareSelected: NoMessageValues;
  loadFailed: NoMessageValues;
  deleteFailed: NoMessageValues;
  compareAction: NoMessageValues;
  viewAction: NoMessageValues;
  returnToCurrent: NoMessageValues;
  viewingPrevious: NoMessageValues;
  loading: NoMessageValues;
  emptyTitle: NoMessageValues;
  emptyBody: NoMessageValues;
  columnVersion: NoMessageValues;
  columnConfigProfile: NoMessageValues;
  columnPages: NoMessageValues;
  columnActions: NoMessageValues;
  currentBadge: NoMessageValues;
  notApplicable: NoMessageValues;
  actionsAriaLabel: { readonly runId: string };
  viewing: NoMessageValues;
  view: NoMessageValues;
  shownReason: NoMessageValues;
  deleteVersion: NoMessageValues;
  cannotDeleteCurrent: NoMessageValues;
  compareTitle: NoMessageValues;
  close: NoMessageValues;
  comparing: NoMessageValues;
  earlierProfile: { readonly config: string };
  laterProfile: { readonly config: string };
  identical: NoMessageValues;
  noFieldChanges: NoMessageValues;
  diffColumnField: NoMessageValues;
  diffColumnChange: NoMessageValues;
  diffColumnEarlier: NoMessageValues;
  diffColumnLater: NoMessageValues;
  deleteTitle: NoMessageValues;
  cancel: NoMessageValues;
  delete: NoMessageValues;
  deleteWarning: NoMessageValues;
  deleteConfirm: { readonly when: string | undefined; readonly strong: MessageTag };
}>({
  title: {
    id: 'documents.versions.title',
    defaultMessage: 'Version History',
    description: 'Heading of the Version History panel on the document page.',
  },
  description: {
    id: 'documents.versions.description',
    defaultMessage:
      'Each successful processing run of this document is retained as a version. Compare any two, or delete old versions to reclaim storage.',
    description: 'Description under the Version History heading.',
  },
  refresh: {
    id: 'documents.versions.refresh',
    defaultMessage: 'Refresh',
    description: 'Button that reloads the version list.',
  },
  compareSelected: {
    id: 'documents.versions.compareSelected',
    defaultMessage: 'Compare selected',
    description: 'Button that compares the two versions selected in the table.',
  },
  loadFailed: {
    id: 'documents.versions.loadFailed',
    defaultMessage: 'Failed to load version history',
    description: 'Error shown when the version list cannot be loaded.',
  },
  deleteFailed: {
    id: 'documents.versions.deleteFailed',
    defaultMessage: 'Failed to delete version',
    description: 'Error shown when a version cannot be deleted.',
  },
  compareAction: {
    id: 'documents.versions.compareAction',
    defaultMessage: 'compare versions',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when two versions cannot be compared.',
  },
  viewAction: {
    id: 'documents.versions.viewAction',
    defaultMessage: 'load this version',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when a version cannot be loaded for viewing.',
  },
  returnToCurrent: {
    id: 'documents.versions.returnToCurrent',
    defaultMessage: 'Return to current version',
    description: 'Button that returns the document page to the current version.',
  },
  viewingPrevious: {
    id: 'documents.versions.viewingPrevious',
    defaultMessage: "Viewing a previous version (read-only). Extraction results and page text shown below reflect this version's output.",
    description: 'Notice shown while the document page shows a previous version.',
  },
  loading: {
    id: 'documents.versions.loading',
    defaultMessage: 'Loading version history...',
    description: 'Shown in the version table while the version list loads.',
  },
  emptyTitle: {
    id: 'documents.versions.empty.title',
    defaultMessage: 'No versions yet',
    description: 'Heading of the version table when the document has no version.',
  },
  emptyBody: {
    id: 'documents.versions.empty.body',
    defaultMessage: 'A version is recorded each time this document completes processing.',
    description: 'Text of the version table when the document has no version.',
  },
  columnVersion: {
    id: 'documents.versions.column.version',
    defaultMessage: 'Version',
    description: 'Column header of the version table: which version the row is.',
  },
  columnConfigProfile: {
    id: 'documents.versions.column.configProfile',
    defaultMessage: 'Config Profile',
    description: 'Column header of the version table: the configuration profile the version was processed with.',
  },
  columnPages: {
    id: 'documents.versions.column.pages',
    defaultMessage: 'Pages',
    description: 'Column header of the version table: the number of pages.',
  },
  columnActions: {
    id: 'documents.versions.column.actions',
    defaultMessage: 'Actions',
    description: 'Column header of the version table: the row menu.',
  },
  currentBadge: {
    id: 'documents.versions.currentBadge',
    defaultMessage: 'Current',
    description: 'Badge on the newest version, the one the document page shows.',
  },
  notApplicable: {
    id: 'documents.versions.notApplicable',
    defaultMessage: 'N/A',
    description: 'Shown when a version has no configuration profile recorded.',
  },
  actionsAriaLabel: {
    id: 'documents.versions.actionsAriaLabel',
    defaultMessage: 'Actions for version {runId}',
    description: "Screen-reader label of a version row's menu. {runId} is the version's run id.",
  },
  viewing: {
    id: 'documents.versions.viewing',
    defaultMessage: 'Viewing',
    description: 'Disabled menu item on the version the document page shows.',
  },
  view: {
    id: 'documents.versions.view',
    defaultMessage: 'View',
    description: 'Menu item that shows the version on the document page.',
  },
  shownReason: {
    id: 'documents.versions.shownReason',
    defaultMessage: 'This version is shown',
    description: 'Why the View menu item is disabled: the page shows this version.',
  },
  deleteVersion: {
    id: 'documents.versions.deleteVersion',
    defaultMessage: 'Delete version',
    description: 'Menu item, for administrators, that deletes the version.',
  },
  cannotDeleteCurrent: {
    id: 'documents.versions.cannotDeleteCurrent',
    defaultMessage: 'Cannot delete the current version',
    description: 'Why the Delete version menu item is disabled on the newest version.',
  },
  compareTitle: {
    id: 'documents.versions.compareTitle',
    defaultMessage: 'Compare versions',
    description: 'Title of the dialog that compares two versions.',
  },
  close: {
    id: 'documents.versions.close',
    defaultMessage: 'Close',
    description: 'Button that closes the compare dialog.',
  },
  comparing: {
    id: 'documents.versions.comparing',
    defaultMessage: 'Comparing extraction results...',
    description: 'Shown in the compare dialog while the two versions are compared.',
  },
  earlierProfile: {
    id: 'documents.versions.earlierProfile',
    defaultMessage: 'Earlier ({config})',
    description: 'Label of the earlier version in the compare dialog. {config} is its configuration profile, or the N/A text.',
  },
  laterProfile: {
    id: 'documents.versions.laterProfile',
    defaultMessage: 'Later ({config})',
    description: 'Label of the later version in the compare dialog. {config} is its configuration profile, or the N/A text.',
  },
  identical: {
    id: 'documents.versions.identical',
    defaultMessage: 'The extraction results are identical across these two versions.',
    description: 'Shown in the compare dialog when the two versions have the same extraction results.',
  },
  noFieldChanges: {
    id: 'documents.versions.noFieldChanges',
    defaultMessage: 'No field-level changes.',
    description: 'Shown in the compare dialog for a section with no changed field.',
  },
  diffColumnField: {
    id: 'documents.versions.diffColumn.field',
    defaultMessage: 'Field',
    description: 'Column header of the compare table: the field that changed.',
  },
  diffColumnChange: {
    id: 'documents.versions.diffColumn.change',
    defaultMessage: 'Change',
    description: 'Column header of the compare table: the kind of change.',
  },
  diffColumnEarlier: {
    id: 'documents.versions.diffColumn.earlier',
    defaultMessage: 'Earlier',
    description: "Column header of the compare table: the earlier version's value.",
  },
  diffColumnLater: {
    id: 'documents.versions.diffColumn.later',
    defaultMessage: 'Later',
    description: "Column header of the compare table: the later version's value.",
  },
  deleteTitle: {
    id: 'documents.versions.deleteTitle',
    defaultMessage: 'Delete version',
    description: 'Title of the dialog that deletes a version.',
  },
  cancel: {
    id: 'documents.versions.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the delete dialog without deleting.',
  },
  delete: {
    id: 'documents.versions.delete',
    defaultMessage: 'Delete',
    description: 'Confirm button of the delete dialog: deletes the version.',
  },
  deleteWarning: {
    id: 'documents.versions.deleteWarning',
    defaultMessage: 'This permanently deletes the pinned output object versions for this run. This cannot be undone.',
    description: 'Warning in the delete dialog.',
  },
  deleteConfirm: {
    id: 'documents.versions.deleteConfirm',
    defaultMessage: 'Delete version <strong>{when}</strong>?',
    description: "Question in the delete dialog. {when} is the version's completion date and time, or its run id when it has none.",
  },
});

/** The section statuses the versions API returns for a compared section (document_versions_resolver). */
type SectionStatusValue = 'changed' | 'only_in_a' | 'only_in_b' | 'identical';

/** How each compared section's status reads, keyed by the value the API returns. */
const sectionStatusMessages = defineMessages<Record<SectionStatusValue, NoMessageValues>>({
  changed: {
    id: 'documents.versions.diffStatus.changed',
    defaultMessage: 'Changed',
    description: 'Status of a compared section whose extraction results differ between the two versions.',
  },
  only_in_a: {
    id: 'documents.versions.diffStatus.onlyInA',
    defaultMessage: 'Only in earlier version',
    description: 'Status of a compared section that only the earlier version has.',
  },
  only_in_b: {
    id: 'documents.versions.diffStatus.onlyInB',
    defaultMessage: 'Only in later version',
    description: 'Status of a compared section that only the later version has.',
  },
  identical: {
    id: 'documents.versions.diffStatus.identical',
    defaultMessage: 'Identical',
    description: 'Status of a compared section whose extraction results are the same in both versions.',
  },
});

/** The change types the versions API returns for a compared field (document_versions_resolver). */
type ChangeTypeValue = 'added' | 'removed' | 'length_changed' | 'changed';

/**
 * How each change type reads in the compare table, keyed by the value the API returns. The
 * English IS that value, so the Change column reads as before; a translation may spell it out.
 * A value missing here is shown as it is.
 */
const changeTypeMessages = defineMessages<Record<ChangeTypeValue, NoMessageValues>>({
  added: {
    id: 'documents.versions.changeType.added',
    defaultMessage: 'added',
    description:
      'Change type of a field that only the later version has. English shows the value as the API returns it; a translation may spell it out.',
  },
  removed: {
    id: 'documents.versions.changeType.removed',
    defaultMessage: 'removed',
    description:
      'Change type of a field that only the earlier version has. English shows the value as the API returns it; a translation may spell it out.',
  },
  length_changed: {
    id: 'documents.versions.changeType.lengthChanged',
    defaultMessage: 'length_changed',
    description:
      'Change type of a list whose length differs between the versions. English shows the value as the API returns it; a translation may spell it out.',
  },
  changed: {
    id: 'documents.versions.changeType.changed',
    defaultMessage: 'changed',
    description:
      'Change type of a field whose value differs between the versions. English shows the value as the API returns it; a translation may spell it out.',
  },
});

const isChangeTypeValue = (value: string): value is ChangeTypeValue => Object.hasOwn(changeTypeMessages, value);

const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

const formatValue = (v: unknown): string => {
  if (v === null || v === undefined) return '∅';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
};

// A status missing from the switch reads as Identical, as it always has.
const statusIndicator = (status: string): React.JSX.Element => {
  switch (status) {
    case 'changed':
      return (
        <StatusIndicator type="warning">
          <FormattedMessage {...sectionStatusMessages.changed} />
        </StatusIndicator>
      );
    case 'only_in_a':
      return (
        <StatusIndicator type="info">
          <FormattedMessage {...sectionStatusMessages.only_in_a} />
        </StatusIndicator>
      );
    case 'only_in_b':
      return (
        <StatusIndicator type="info">
          <FormattedMessage {...sectionStatusMessages.only_in_b} />
        </StatusIndicator>
      );
    default:
      return (
        <StatusIndicator type="success">
          <FormattedMessage {...sectionStatusMessages.identical} />
        </StatusIndicator>
      );
  }
};

const DocumentVersionsPanel = ({
  objectKey,
  viewingRunId = null,
  onViewVersion,
  completionTime = null,
}: DocumentVersionsPanelProps): React.JSX.Element => {
  const intl = useIntl();
  const { formatDateTime } = useFormatters();
  const { isAdmin } = useUserRole();
  const [versions, setVersions] = useState<DocumentVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<PanelError | null>(null);
  const [loadingVersionId, setLoadingVersionId] = useState<string | null>(null);

  // Compare state
  const [selectedForCompare, setSelectedForCompare] = useState<DocumentVersion[]>([]);
  const [compareResult, setCompareResult] = useState<CompareResult | null>(null);
  const [isComparing, setIsComparing] = useState(false);
  const [compareModalVisible, setCompareModalVisible] = useState(false);

  // Delete state
  const [deleteTarget, setDeleteTarget] = useState<DocumentVersion | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const loadVersions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await client.graphql({ query: listDocumentVersions, variables: { objectKey } });
      setVersions((result.data.listDocumentVersions ?? []) as DocumentVersion[]);
    } catch (err) {
      logger.error('Error loading document versions', err);
      setError({ message: messages.loadFailed });
    } finally {
      setLoading(false);
    }
  }, [objectKey]);

  useEffect(() => {
    loadVersions();
  }, [loadVersions, completionTime]);

  const handleCompare = async () => {
    if (selectedForCompare.length !== 2) return;
    // Compare oldest→newest so "a" is the earlier run.
    const [x, y] = selectedForCompare;
    const [a, b] = (x.RunId < y.RunId ? [x, y] : [y, x]) as [DocumentVersion, DocumentVersion];
    setIsComparing(true);
    setCompareModalVisible(true);
    setCompareResult(null);
    try {
      const result = await client.graphql({
        query: compareDocumentVersions,
        variables: { objectKey, runIdA: a.RunId, runIdB: b.RunId },
      });
      const raw = result.data.compareDocumentVersions;
      setCompareResult(typeof raw === 'string' ? JSON.parse(raw) : raw);
    } catch (err) {
      logger.error('Error comparing versions', err);
      setError({ action: messages.compareAction, cause: err });
      setCompareModalVisible(false);
    } finally {
      setIsComparing(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      await client.graphql({
        query: deleteDocumentVersion,
        variables: { objectKey, runId: deleteTarget.RunId },
      });
      // If the version being viewed was just deleted, return to current.
      if (viewingRunId === deleteTarget.RunId) {
        onViewVersion?.(null, null);
      }
      setDeleteTarget(null);
      await loadVersions();
    } catch (err) {
      logger.error('Error deleting version', err);
      setError({ message: messages.deleteFailed });
    } finally {
      setIsDeleting(false);
    }
  };

  // The newest run is the one currently reflected in the live document view.
  const currentRunId = versions.length > 0 ? versions[0].RunId : null;

  // Fetch a version's manifest (Files: Key + VersionId) and hand it to the
  // parent so the page renders that run's pinned bytes. Passing the newest
  // run returns to the live/current view (null).
  const handleView = async (version: DocumentVersion) => {
    if (!onViewVersion) return;
    if (version.RunId === currentRunId) {
      onViewVersion(null, null);
      return;
    }
    setLoadingVersionId(version.RunId);
    setError(null);
    try {
      const result = await client.graphql({
        query: getDocumentVersion,
        variables: { objectKey, runId: version.RunId },
      });
      const detail = result.data.getDocumentVersion as DocumentVersionDetail | null;
      onViewVersion(version.RunId, detail);
    } catch (err) {
      logger.error('Error loading version for viewing', err);
      setError({ action: messages.viewAction, cause: err });
    } finally {
      setLoadingVersionId(null);
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={versions.length ? `(${versions.length})` : undefined}
          description={intl.formatMessage(messages.description)}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={loadVersions} loading={loading}>
                <FormattedMessage {...messages.refresh} />
              </Button>
              <Button variant="primary" disabled={selectedForCompare.length !== 2} onClick={handleCompare}>
                <FormattedMessage {...messages.compareSelected} />
              </Button>
            </SpaceBetween>
          }
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <SpaceBetween size="m">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {'action' in error ? describeApiError(error.cause, intl.formatMessage(error.action)) : <FormattedMessage {...error.message} />}
          </Alert>
        )}
        {viewingRunId && (
          <Alert
            type="info"
            action={
              <Button onClick={() => onViewVersion?.(null, null)}>
                <FormattedMessage {...messages.returnToCurrent} />
              </Button>
            }
          >
            <FormattedMessage {...messages.viewingPrevious} />
          </Alert>
        )}
        <Table
          variant="embedded"
          loading={loading}
          loadingText={intl.formatMessage(messages.loading)}
          selectionType="multi"
          isItemDisabled={() => false}
          selectedItems={selectedForCompare}
          onSelectionChange={({ detail }) => {
            // Cap selection at 2 for comparison.
            const items = detail.selectedItems as DocumentVersion[];
            setSelectedForCompare(items.slice(-2));
          }}
          trackBy="RunId"
          empty={
            <Box textAlign="center" color="inherit">
              <b>
                <FormattedMessage {...messages.emptyTitle} />
              </b>
              <Box variant="p" color="inherit">
                <FormattedMessage {...messages.emptyBody} />
              </Box>
            </Box>
          }
          columnDefinitions={[
            {
              id: 'version',
              header: intl.formatMessage(messages.columnVersion),
              cell: (item: DocumentVersion) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <span>{formatStoredTime(item.CompletionTime, formatDateTime) || item.RunId}</span>
                  {item.RunId === currentRunId && (
                    <Badge color="green">
                      <FormattedMessage {...messages.currentBadge} />
                    </Badge>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: 'configVersion',
              header: intl.formatMessage(messages.columnConfigProfile),
              cell: (item: DocumentVersion) => item.ConfigVersion || intl.formatMessage(messages.notApplicable),
            },
            {
              id: 'pageCount',
              header: intl.formatMessage(messages.columnPages),
              cell: (item: DocumentVersion) => item.PageCount ?? '-',
            },
            {
              id: 'actions',
              header: intl.formatMessage(messages.columnActions),
              // One ⋮ menu per row: View (or Viewing, on the version shown) and, for an
              // Admin, Delete version.
              width: 100,
              cell: (item: DocumentVersion) => {
                const isViewing = viewingRunId === item.RunId || (viewingRunId === null && item.RunId === currentRunId);
                return (
                  <ButtonDropdown
                    variant="inline-icon"
                    // Render the menu in a portal so it isn't clipped by the
                    // table row/container overflow.
                    expandToViewport
                    loading={loadingVersionId === item.RunId}
                    ariaLabel={intl.formatMessage(messages.actionsAriaLabel, { runId: item.RunId })}
                    items={[
                      {
                        id: 'view',
                        text: intl.formatMessage(isViewing ? messages.viewing : messages.view),
                        disabled: isViewing || !onViewVersion,
                        disabledReason: isViewing ? intl.formatMessage(messages.shownReason) : undefined,
                      },
                      ...(isAdmin
                        ? [
                            {
                              id: 'delete',
                              text: intl.formatMessage(messages.deleteVersion),
                              disabled: item.RunId === currentRunId,
                              disabledReason: intl.formatMessage(messages.cannotDeleteCurrent),
                            },
                          ]
                        : []),
                    ]}
                    onItemClick={({ detail }) => {
                      if (detail.id === 'view') handleView(item);
                      else if (detail.id === 'delete') setDeleteTarget(item);
                    }}
                  />
                );
              },
            },
          ]}
          items={versions}
        />
      </SpaceBetween>

      {/* Compare modal */}
      <Modal
        visible={compareModalVisible}
        onDismiss={() => setCompareModalVisible(false)}
        size="large"
        header={intl.formatMessage(messages.compareTitle)}
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setCompareModalVisible(false)}>
              <FormattedMessage {...messages.close} />
            </Button>
          </Box>
        }
      >
        {isComparing && (
          <StatusIndicator type="loading">
            <FormattedMessage {...messages.comparing} />
          </StatusIndicator>
        )}
        {!isComparing && compareResult && (
          <SpaceBetween size="m">
            <Box>
              <SpaceBetween direction="horizontal" size="l">
                <div>
                  <Box color="text-label">
                    <FormattedMessage
                      {...messages.earlierProfile}
                      values={{ config: compareResult.configVersionA || intl.formatMessage(messages.notApplicable) }}
                    />
                  </Box>
                  <div>{formatStoredTime(compareResult.completionTimeA, formatDateTime)}</div>
                </div>
                <div>
                  <Box color="text-label">
                    <FormattedMessage
                      {...messages.laterProfile}
                      values={{ config: compareResult.configVersionB || intl.formatMessage(messages.notApplicable) }}
                    />
                  </Box>
                  <div>{formatStoredTime(compareResult.completionTimeB, formatDateTime)}</div>
                </div>
              </SpaceBetween>
            </Box>
            {compareResult.identical ? (
              <Alert type="success">
                <FormattedMessage {...messages.identical} />
              </Alert>
            ) : (
              compareResult.sections.map((section) => (
                <ExpandableSection
                  key={section.section}
                  headerText={section.section}
                  headerActions={statusIndicator(section.status)}
                  defaultExpanded={section.status === 'changed'}
                >
                  {section.changes.length === 0 ? (
                    <Box color="text-status-inactive">
                      <FormattedMessage {...messages.noFieldChanges} />
                    </Box>
                  ) : (
                    <Table
                      variant="embedded"
                      columnDefinitions={[
                        { id: 'path', header: intl.formatMessage(messages.diffColumnField), cell: (c: VersionChange) => c.path },
                        {
                          id: 'type',
                          header: intl.formatMessage(messages.diffColumnChange),
                          cell: (c: VersionChange) =>
                            isChangeTypeValue(c.type) ? <FormattedMessage {...changeTypeMessages[c.type]} /> : c.type,
                        },
                        { id: 'a', header: intl.formatMessage(messages.diffColumnEarlier), cell: (c: VersionChange) => formatValue(c.a) },
                        { id: 'b', header: intl.formatMessage(messages.diffColumnLater), cell: (c: VersionChange) => formatValue(c.b) },
                      ]}
                      items={section.changes}
                    />
                  )}
                </ExpandableSection>
              ))
            )}
          </SpaceBetween>
        )}
      </Modal>

      {/* Delete confirmation */}
      <Modal
        visible={deleteTarget !== null}
        onDismiss={() => setDeleteTarget(null)}
        header={intl.formatMessage(messages.deleteTitle)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteTarget(null)} disabled={isDeleting}>
                <FormattedMessage {...messages.cancel} />
              </Button>
              <Button variant="primary" onClick={handleDelete} loading={isDeleting}>
                <FormattedMessage {...messages.delete} />
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Alert type="warning">
            <FormattedMessage {...messages.deleteWarning} />
          </Alert>
          <p>
            <FormattedMessage
              {...messages.deleteConfirm}
              values={{ when: formatStoredTime(deleteTarget?.CompletionTime, formatDateTime) || deleteTarget?.RunId, strong }}
            />
          </p>
        </SpaceBetween>
      </Modal>
    </Container>
  );
};

export default DocumentVersionsPanel;
