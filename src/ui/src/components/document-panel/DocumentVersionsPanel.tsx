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
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { listDocumentVersions, getDocumentVersion, compareDocumentVersions, deleteDocumentVersion } from '../../graphql/generated';
import useUserRole from '../../hooks/use-user-role';

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

const formatValue = (v: unknown): string => {
  if (v === null || v === undefined) return '∅';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
};

const statusIndicator = (status: string): React.JSX.Element => {
  switch (status) {
    case 'changed':
      return <StatusIndicator type="warning">Changed</StatusIndicator>;
    case 'only_in_a':
      return <StatusIndicator type="info">Only in earlier version</StatusIndicator>;
    case 'only_in_b':
      return <StatusIndicator type="info">Only in later version</StatusIndicator>;
    default:
      return <StatusIndicator type="success">Identical</StatusIndicator>;
  }
};

const DocumentVersionsPanel = ({ objectKey, viewingRunId = null, onViewVersion }: DocumentVersionsPanelProps): React.JSX.Element => {
  const { isAdmin } = useUserRole();
  const [versions, setVersions] = useState<DocumentVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
      setError('Failed to load version history');
    } finally {
      setLoading(false);
    }
  }, [objectKey]);

  useEffect(() => {
    loadVersions();
  }, [loadVersions]);

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
      setError('Failed to compare versions');
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
      setError('Failed to delete version');
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
      setError('Failed to load version');
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
          description="Each successful processing run of this document is retained as a version. Compare any two, or delete old versions to reclaim storage."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={loadVersions} loading={loading}>
                Refresh
              </Button>
              <Button variant="primary" disabled={selectedForCompare.length !== 2} onClick={handleCompare}>
                Compare selected
              </Button>
            </SpaceBetween>
          }
        >
          Version History
        </Header>
      }
    >
      <SpaceBetween size="m">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}
        {viewingRunId && (
          <Alert type="info" action={<Button onClick={() => onViewVersion?.(null, null)}>Return to current version</Button>}>
            Viewing a previous version (read-only). Extraction results and page text shown below reflect this version&apos;s output.
          </Alert>
        )}
        <Table
          variant="embedded"
          loading={loading}
          loadingText="Loading version history..."
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
              <b>No versions yet</b>
              <Box variant="p" color="inherit">
                A version is recorded each time this document completes processing.
              </Box>
            </Box>
          }
          columnDefinitions={[
            {
              id: 'version',
              header: 'Version',
              cell: (item: DocumentVersion) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <span>{item.CompletionTime || item.RunId}</span>
                  {item.RunId === currentRunId && <Badge color="green">Current</Badge>}
                </SpaceBetween>
              ),
            },
            {
              id: 'configVersion',
              header: 'Config Profile',
              cell: (item: DocumentVersion) => item.ConfigVersion || 'N/A',
            },
            {
              id: 'pageCount',
              header: 'Pages',
              cell: (item: DocumentVersion) => item.PageCount ?? '-',
            },
            {
              id: 'actions',
              header: 'Actions',
              cell: (item: DocumentVersion) => {
                const isViewing = viewingRunId === item.RunId || (viewingRunId === null && item.RunId === currentRunId);
                return (
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      variant="inline-link"
                      loading={loadingVersionId === item.RunId}
                      disabled={isViewing || !onViewVersion}
                      onClick={() => handleView(item)}
                    >
                      {isViewing ? 'Viewing' : 'View'}
                    </Button>
                    {isAdmin && (
                      <ButtonDropdown
                        variant="inline-icon"
                        // Render the menu in a portal so it isn't clipped by the
                        // table row/container overflow.
                        expandToViewport
                        ariaLabel={`Actions for version ${item.RunId}`}
                        items={[
                          {
                            id: 'delete',
                            text: 'Delete version',
                            disabled: item.RunId === currentRunId,
                            disabledReason: 'Cannot delete the current version',
                          },
                        ]}
                        onItemClick={({ detail }) => {
                          if (detail.id === 'delete') setDeleteTarget(item);
                        }}
                      />
                    )}
                  </SpaceBetween>
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
        header="Compare versions"
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setCompareModalVisible(false)}>
              Close
            </Button>
          </Box>
        }
      >
        {isComparing && <StatusIndicator type="loading">Comparing extraction results...</StatusIndicator>}
        {!isComparing && compareResult && (
          <SpaceBetween size="m">
            <Box>
              <SpaceBetween direction="horizontal" size="l">
                <div>
                  <Box color="text-label">Earlier ({compareResult.configVersionA || 'N/A'})</Box>
                  <div>{compareResult.completionTimeA}</div>
                </div>
                <div>
                  <Box color="text-label">Later ({compareResult.configVersionB || 'N/A'})</Box>
                  <div>{compareResult.completionTimeB}</div>
                </div>
              </SpaceBetween>
            </Box>
            {compareResult.identical ? (
              <Alert type="success">The extraction results are identical across these two versions.</Alert>
            ) : (
              compareResult.sections.map((section) => (
                <ExpandableSection
                  key={section.section}
                  headerText={section.section}
                  headerActions={statusIndicator(section.status)}
                  defaultExpanded={section.status === 'changed'}
                >
                  {section.changes.length === 0 ? (
                    <Box color="text-status-inactive">No field-level changes.</Box>
                  ) : (
                    <Table
                      variant="embedded"
                      columnDefinitions={[
                        { id: 'path', header: 'Field', cell: (c: VersionChange) => c.path },
                        { id: 'type', header: 'Change', cell: (c: VersionChange) => c.type },
                        { id: 'a', header: 'Earlier', cell: (c: VersionChange) => formatValue(c.a) },
                        { id: 'b', header: 'Later', cell: (c: VersionChange) => formatValue(c.b) },
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
        header="Delete version"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteTarget(null)} disabled={isDeleting}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDelete} loading={isDeleting}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Alert type="warning">This permanently deletes the pinned output object versions for this run. This cannot be undone.</Alert>
          <p>
            Delete version <strong>{deleteTarget?.CompletionTime || deleteTarget?.RunId}</strong>?
          </p>
        </SpaceBetween>
      </Modal>
    </Container>
  );
};

export default DocumentVersionsPanel;
