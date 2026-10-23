// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useMemo } from 'react';
import {
  Box,
  SpaceBetween,
  Container,
  Header,
  ExpandableSection,
  Badge,
  Alert,
  Table,
  StatusIndicator,
} from '@cloudscape-design/components';

interface DiffChange {
  originalValue: unknown;
  newValue: unknown;
}

interface EditEntry {
  timestamp: string;
  editedBy?: string;
  predictionEdits?: {
    changeCount: number;
    diffs?: Record<string, DiffChange>;
  };
  baselineEdits?: {
    changeCount: number;
    diffs?: Record<string, DiffChange>;
  };
}

interface CombinedHistoryEntry extends EditEntry {
  source: 'prediction' | 'baseline';
  id: string;
}

interface DiffItem {
  fieldPath: string;
  originalValue: unknown;
  newValue: unknown;
}

interface EditHistoryTabProps {
  predictionData: Record<string, unknown> | null;
  baselineData: Record<string, unknown> | null;
  /** Height of the scrolling area; the default fills the editor pane. A dialog passes its own. */
  height?: string;
}

/**
 * EditHistoryTab - Shows timeline of all edits made to this document section
 * Parses _editHistory array from the saved JSON files
 */
const EditHistoryTab = ({ predictionData, baselineData, height = 'calc(100vh - 280px)' }: EditHistoryTabProps): React.JSX.Element => {
  const [expandedEntries, setExpandedEntries] = useState<Set<string>>(new Set());

  // Extract edit history from prediction data
  const predictionHistory = useMemo((): EditEntry[] => {
    return (predictionData?._editHistory as EditEntry[]) || [];
  }, [predictionData]);

  // Extract edit history from baseline data
  const baselineHistory = useMemo((): EditEntry[] => {
    return (baselineData?._editHistory as EditEntry[]) || [];
  }, [baselineData]);

  // Merge and sort all history entries by timestamp
  const combinedHistory = useMemo((): CombinedHistoryEntry[] => {
    const allEntries: CombinedHistoryEntry[] = [];

    // Add prediction history entries
    predictionHistory.forEach((entry, index) => {
      allEntries.push({
        ...entry,
        source: 'prediction',
        id: `pred-${index}`,
      });
    });

    // Add baseline history entries (may have some overlap with prediction history)
    baselineHistory.forEach((entry, index) => {
      // Check if this is a duplicate (same timestamp + editedBy)
      const isDuplicate = allEntries.some((e) => e.timestamp === entry.timestamp && e.editedBy === entry.editedBy);
      if (!isDuplicate) {
        allEntries.push({
          ...entry,
          source: 'baseline',
          id: `base-${index}`,
        });
      }
    });

    // Sort by timestamp descending (most recent first)
    return allEntries.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
  }, [predictionHistory, baselineHistory]);

  // Toggle expanded state
  const toggleExpanded = (id: string) => {
    setExpandedEntries((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(id)) {
        newSet.delete(id);
      } else {
        newSet.add(id);
      }
      return newSet;
    });
  };

  // Format timestamp for display
  const formatTimestamp = (timestamp: string): string => {
    try {
      const date = new Date(timestamp);
      return date.toLocaleString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    } catch (e) {
      return timestamp;
    }
  };

  // Render diff table for changes
  const renderDiffTable = (diffs: Record<string, DiffChange> | undefined, type: string): React.JSX.Element => {
    if (!diffs || Object.keys(diffs).length === 0) {
      return <Box color="text-body-secondary">No {type} changes</Box>;
    }

    const items: DiffItem[] = Object.entries(diffs).map(([fieldPath, change]) => ({
      fieldPath,
      originalValue: change.originalValue,
      newValue: change.newValue,
    }));

    return (
      <Table
        columnDefinitions={[
          {
            id: 'fieldPath',
            header: 'Field',
            cell: (item) => <code>{item.fieldPath}</code>,
            width: 200,
          },
          {
            id: 'originalValue',
            header: 'Original Value',
            cell: (item) => (
              <Box color="text-body-secondary">
                {typeof item.originalValue === 'object' ? JSON.stringify(item.originalValue) : String(item.originalValue ?? 'null')}
              </Box>
            ),
          },
          {
            id: 'arrow',
            header: '',
            cell: () => '→',
            width: 30,
          },
          {
            id: 'newValue',
            header: 'New Value',
            cell: (item) => (
              <Box fontWeight="bold">
                {typeof item.newValue === 'object' ? JSON.stringify(item.newValue) : String(item.newValue ?? 'null')}
              </Box>
            ),
          },
        ]}
        items={items}
        variant="embedded"
        stripedRows
      />
    );
  };

  // If no history, show empty state
  if (combinedHistory.length === 0) {
    return (
      <div style={{ height, padding: '16px' }}>
        <Alert type="info" header="No Edit History">
          <SpaceBetween size="s">
            <Box>No edit history is available for this document section.</Box>
            <Box>
              Edit history is recorded when changes are saved using the Visual Editor or JSON Editor. Each edit session creates a
              timestamped entry with the user and changes made.
            </Box>
          </SpaceBetween>
        </Alert>
      </div>
    );
  }

  return (
    <div style={{ height, padding: '16px', overflow: 'auto' }}>
      <SpaceBetween size="m">
        <Alert type="info">
          <strong>Edit History</strong> - View all changes made to this section.
          {combinedHistory.length} edit session{combinedHistory.length !== 1 ? 's' : ''} found.
        </Alert>

        <Container
          header={
            <Header variant="h3" counter={`(${combinedHistory.length})`}>
              Change Timeline
            </Header>
          }
        >
          <SpaceBetween size="m">
            {combinedHistory.map((entry) => {
              const predEditCount = entry.predictionEdits?.changeCount || 0;
              const baseEditCount = entry.baselineEdits?.changeCount || 0;
              const totalChanges = predEditCount + baseEditCount;
              const isExpanded = expandedEntries.has(entry.id);

              return (
                <ExpandableSection
                  key={entry.id}
                  expanded={isExpanded}
                  onChange={() => toggleExpanded(entry.id)}
                  headerText={
                    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                      <Box fontWeight="bold">{formatTimestamp(entry.timestamp)}</Box>
                      <Box color="text-body-secondary">by {entry.editedBy || 'Unknown User'}</Box>
                      <Badge color={totalChanges > 0 ? 'blue' : 'grey'}>
                        {totalChanges} change{totalChanges !== 1 ? 's' : ''}
                      </Badge>
                    </SpaceBetween>
                  }
                  variant="container"
                >
                  <SpaceBetween size="l">
                    {/* Summary */}
                    <Box>
                      <SpaceBetween direction="horizontal" size="m">
                        {predEditCount > 0 && (
                          <StatusIndicator type="info">
                            {predEditCount} prediction edit{predEditCount !== 1 ? 's' : ''}
                          </StatusIndicator>
                        )}
                        {baseEditCount > 0 && (
                          <StatusIndicator type="warning">
                            {baseEditCount} baseline edit{baseEditCount !== 1 ? 's' : ''}
                          </StatusIndicator>
                        )}
                        {totalChanges === 0 && <StatusIndicator type="stopped">No changes recorded</StatusIndicator>}
                      </SpaceBetween>
                    </Box>

                    {/* Prediction Changes */}
                    {predEditCount > 0 && (
                      <Box>
                        <Box variant="h4" margin={{ bottom: 's' }}>
                          Prediction Changes
                        </Box>
                        {renderDiffTable(entry.predictionEdits?.diffs, 'prediction')}
                      </Box>
                    )}

                    {/* Baseline Changes */}
                    {baseEditCount > 0 && (
                      <Box>
                        <Box variant="h4" margin={{ bottom: 's' }}>
                          Baseline Changes
                        </Box>
                        {renderDiffTable(entry.baselineEdits?.diffs, 'baseline')}
                      </Box>
                    )}
                  </SpaceBetween>
                </ExpandableSection>
              );
            })}
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    </div>
  );
};

export default EditHistoryTab;
