// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useMemo } from 'react';
import {
  Box,
  Container,
  SpaceBetween,
  Table,
  StatusIndicator,
  Button,
  ButtonDropdown,
  Header,
  FormField,
  Select,
  Input,
  Textarea,
  Modal,
  Alert,
  Badge,
  Popover,
} from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import FileViewer from '../document-viewer/JSONViewer';
import { getSectionConfidenceAlertCount, getSectionConfidenceAlerts } from '../common/confidence-alerts-utils';
import { SectionClassMismatch } from '../common/ClassMismatchIndicator';
import ClassNameText from '../common/ClassNameText';
import { EMPTY_CLASSIFICATION_INDEX, type ClassificationIndex } from '../common/classification-comparison-utils';
import { getConfigClassOptions } from '../common/config-class-options';
import PageGroupingEditor from '../common/PageGroupingEditor';
import type { GroupedSection } from '../common/section-grouping';
import { updateDocumentSections } from '../../graphql/generated';
import usePageThumbnails from '../../hooks/use-page-thumbnails';
import { getErrorMessage } from '../../utils/errorUtils';
import { getSectionIssueStatus } from '../common/processing-issues-utils';
import type { EditableSection } from '../../types/documents';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import { useDocumentVersion } from '../../contexts/document-version';
import { processChanges, getFilePresignedUrl, skipAllSectionsReview } from '../../graphql/generated';
import { parseHITLReviewHistory } from '../../graphql/awsjson-parsers';

const client = generateClient();
const logger = new ConsoleLogger('SectionsPanel');

/**
 * The row shape for the sections table.
 *
 * Derived from the generated GraphQL `Section` via `EditableSection` (issue
 * #711) — this used to be a second, hand-written interface, so a field added to
 * `schema.graphql` was invisible here even after codegen, with nothing failing
 * to say so. That is why `InstanceCount` needed hand-wiring in two places, and
 * why `Excluded` / `ExclusionReason` / `ConfidenceThresholdAlerts` had already
 * drifted between the two Section shapes.
 */
type SectionItem = EditableSection;

interface PageItem {
  Id: number;
  ImageUri?: string;
  TextUri?: string;
}

interface DocumentItem {
  hitlReviewOwner?: string;
  hitlReviewOwnerEmail?: string;
  hitlTriggered?: boolean;
  hitlStatus?: string;
  hitlSectionsCompleted?: string[];
  hitlSectionsPending?: string[];
  hitlSectionsSkipped?: string[];
  objectStatus?: string;
  ObjectKey?: string;
  objectKey?: string;
  key?: string;
  Key?: string;
  id?: string;
  Id?: string;
  evaluationStatus?: string;
  pages?: Record<string, unknown>[];
}

interface SectionsPanelProps {
  sections?: SectionItem[];
  pages?: PageItem[];
  documentItem?: DocumentItem;
  mergedConfig?: Record<string, unknown> | null;
  onDocumentUpdate?: (updater: (prev: Record<string, unknown>) => Record<string, unknown>) => void;
  /**
   * Ground-truth-vs-predicted classification for this document, loaded once by
   * the document page. Empty (the default) when there is no ground truth, in
   * which case no class is annotated.
   */
  classificationIndex?: ClassificationIndex;
}

// Cell renderer components
const IdCell = ({ item }: { item: SectionItem }): React.JSX.Element => <span>{item.Id}</span>;

// Render the class name, annotated with a "Skipped" badge when the section's
// classification was marked x-aws-idp-exclude-from-processing=true in config,
// and with a mismatch alert when ground truth expects a different class.
const ClassCell = ({
  item,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
}: {
  item: SectionItem;
  classificationIndex?: ClassificationIndex;
}): React.JSX.Element => {
  // An excluded section is not extracted, so comparing its class to ground
  // truth would flag a section nobody scored.
  const mismatch = item.Excluded ? null : (
    <SectionClassMismatch index={classificationIndex} pageNumbers={item.PageIds ?? []} predictedClass={item.Class} />
  );

  // Only rendered when the section holds more than one document, so a normal
  // section is just its class name.
  const instances = <MultiInstanceBadge item={item} />;

  if (item.Excluded) {
    return (
      <SpaceBetween direction="horizontal" size="xs">
        <ClassNameText color="#5f6b7a">{item.Class}</ClassNameText>
        <Badge color="grey">Skipped: {item.ExclusionReason || 'excluded'}</Badge>
      </SpaceBetween>
    );
  }
  if (!mismatch && !instances) return <ClassNameText>{item.Class}</ClassNameText>;
  return (
    <SpaceBetween direction="horizontal" size="xs">
      <ClassNameText>{item.Class}</ClassNameText>
      {instances}
      {mismatch}
    </SpaceBetween>
  );
};

const PageIdsCell = ({ item }: { item: SectionItem }): React.JSX.Element => <span>{item.PageIds.join(', ')}</span>;

// Multi-instance annotation, rendered INSIDE the class cell rather than in a
// column of its own. `InstanceCount` is how many separate documents of this
// section's Class extraction found in it, and it is worth screen space in
// exactly one case:
//   > 1        -> the section holds several distinct documents that
//                 classification did not split apart. A Badge (the same Badge
//                 vocabulary the "Skipped" annotation uses) plus a hover Popover.
//   1, 0, absent -> nothing at all. A column showed "1" on every row of a normal
//                 document and cost width the table did not have (it wrapped its
//                 own header to "Instanc/es" and pushed Actions off the panel).
//                 It also distinguished "1" from "undetermined" — a diagnostic
//                 distinction, still available on the API and in the Processing
//                 Report, that no reader of this table was acting on.
// The *warning* for the unflagged case is owned by the Status column (backend
// raises a `extraction_multi_instance_detected` ProcessingIssue), so this stays
// factual rather than alarming — a class configured for multiple instances is
// working as intended.
//
// EXCEPT for the #753 case, where the count comes from the model's own answer to
// "how many documents are in these pages" and the extra records were NOT
// extracted. "Each one was extracted as its own instance" would be flatly untrue
// there, so the badge reads the section's own
// `extraction_multi_instance_suspected` issue — the authoritative signal, already
// on the row — rather than needing a new `InstanceSource` field plumbed through
// the whole Section chain.
const SUSPECTED_ISSUE_CODE = 'extraction_multi_instance_suspected';

const MultiInstanceBadge = ({ item }: { item: SectionItem }): React.JSX.Element | null => {
  const count = item.InstanceCount ?? 0;

  if (count <= 1) {
    return null;
  }

  const suspected = (item.ProcessingIssues ?? []).some((issue) => issue?.code === SUSPECTED_ISSUE_CODE);

  return (
    <Popover
      dismissButton={false}
      position="top"
      size="medium"
      triggerType="custom"
      header={suspected ? 'Documents may be missing from this section' : 'Multiple documents in one section'}
      content={
        <SpaceBetween size="xs">
          {suspected ? (
            <Box variant="p">
              These pages appear to contain {count} separate {item.Class || 'document'} documents, but only the first was extracted — the
              rest are not in the result.
            </Box>
          ) : (
            <Box variant="p">
              Extraction found {count} separate {item.Class || 'document'} documents in this section. Each one was extracted as its own
              instance.
            </Box>
          )}
          <Box variant="small" color="text-body-secondary">
            {suspected
              ? 'Split the section (classification section splitting), or turn on multi-instance extraction for this class so every document is extracted.'
              : 'If these should be separate sections, review the classification settings for this class.'}
          </Box>
        </SpaceBetween>
      }
    >
      <span style={{ cursor: 'pointer' }}>
        <Badge color={suspected ? 'severity-medium' : 'blue'}>{count}</Badge>
      </span>
    </Popover>
  );
};

// Confidence alerts cell showing only count
const ConfidenceAlertsCell = ({
  item,
  mergedConfig,
}: {
  item: SectionItem;
  mergedConfig: Record<string, unknown> | null | undefined;
}): React.JSX.Element => {
  if (!mergedConfig) {
    // Fallback to original behavior - just show the count as a number
    const count = getSectionConfidenceAlertCount(item);
    return count === 0 ? <StatusIndicator type="success">0</StatusIndicator> : <StatusIndicator type="warning">{count}</StatusIndicator>;
  }

  const alerts = getSectionConfidenceAlerts(item, mergedConfig);
  const alertCount = alerts.length;

  if (alertCount === 0) {
    return <StatusIndicator type="success">0</StatusIndicator>;
  }

  return <StatusIndicator type="warning">{alertCount}</StatusIndicator>;
};

// Processing status cell: a worst-severity StatusIndicator over the section's
// structured ProcessingIssues, wrapped in a hover Popover listing each issue's
// message + root cause. Reuses the getSectionIssueStatus helper (mirrors the
// hitl-status-renderer + confidence-alerts-utils conventions).
const StatusCell = ({ item }: { item: SectionItem }): React.JSX.Element => {
  const issues = item.ProcessingIssues || [];
  const { type, label } = getSectionIssueStatus(item);

  const indicator = <StatusIndicator type={type}>{label}</StatusIndicator>;

  if (issues.length === 0) {
    return indicator;
  }

  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="custom"
      header="Processing issues"
      content={
        <SpaceBetween size="s">
          {issues.map((issue, idx) => (
            <div key={`${issue.code ?? 'issue'}-${issue.message?.slice(0, 24) ?? idx}`}>
              <Box variant="awsui-key-label">
                <StatusIndicator
                  type={
                    (issue.severity || 'info').toLowerCase() === 'error'
                      ? 'error'
                      : (issue.severity || 'info').toLowerCase() === 'warning'
                        ? 'warning'
                        : 'info'
                  }
                >
                  {issue.code || issue.stage || 'issue'}
                </StatusIndicator>
              </Box>
              <Box variant="p">{issue.message}</Box>
              {issue.rootCause && (
                <Box variant="small" color="text-body-secondary">
                  Root cause: {issue.rootCause}
                </Box>
              )}
            </div>
          ))}
        </SpaceBetween>
      }
    >
      <span style={{ cursor: 'pointer' }}>{indicator}</span>
    </Popover>
  );
};

const ActionsCell = ({
  item,
  pages,
  documentItem,
  mergedConfig,
  isSectionCompleted,
  isReviewerOnly,
  isEditModeEnabled,
  // Section navigation props
  allSections = [],
  currentSectionIndex = 0,
  onNavigateToSection,
  onViewerOpen,
  onViewerClose,
  isViewerOpen = false,
}: {
  item: SectionItem;
  pages: PageItem[];
  documentItem: DocumentItem | undefined;
  mergedConfig: Record<string, unknown> | null | undefined;
  isSectionCompleted: boolean;
  isReviewerOnly: boolean;
  isEditModeEnabled: boolean;
  allSections?: SectionItem[];
  currentSectionIndex?: number;
  onNavigateToSection: (index: number) => void;
  onViewerOpen: () => void;
  onViewerClose: () => void;
  isViewerOpen?: boolean;
}) => {
  const [isDownloading, setIsDownloading] = React.useState(false);
  const { settings } = useSettingsContext();

  // Disable View/Edit only if reviewer and no review owner (review not claimed)
  // View Data should always be enabled, Edit Mode requires claimed review
  const _hasReviewOwner = documentItem?.hitlReviewOwner || documentItem?.hitlReviewOwnerEmail;
  const shouldDisableViewEdit = false; // View Data always enabled

  // Check if baseline is available based on evaluation status
  const isBaselineAvailable = documentItem?.evaluationStatus === 'BASELINE_AVAILABLE' || documentItem?.evaluationStatus === 'COMPLETED';

  // Construct baseline URI by replacing output bucket with evaluation baseline bucket
  const constructBaselineUri = (outputUri: string | undefined) => {
    if (!outputUri) return null;

    // Get actual bucket names from settings
    const outputBucketName = settings?.OutputBucket;
    const baselineBucketName = settings?.EvaluationBaselineBucket;

    if (!outputBucketName || !baselineBucketName) {
      logger.error('Bucket names not available in settings');
      logger.debug('Settings:', settings);
      return null;
    }

    // Parse the S3 URI to extract bucket and key
    // Format: s3://bucket-name/path/to/file
    const match = outputUri.match(/^s3:\/\/([^/]+)\/(.+)$/);
    if (!match) {
      logger.error('Invalid S3 URI format:', outputUri);
      return null;
    }

    const [, bucketName, objectKey] = match;

    // Verify this is actually the output bucket before replacing
    if (bucketName !== outputBucketName) {
      logger.warn(`URI bucket (${bucketName}) does not match expected output bucket (${outputBucketName})`);
    }

    // Replace the output bucket with the baseline bucket (same object key)
    const baselineUri = `s3://${baselineBucketName}/${objectKey}`;

    logger.info(`Converted output URI to baseline URI:`);
    logger.info(`  Output: ${outputUri}`);
    logger.info(`  Baseline: ${baselineUri}`);

    return baselineUri;
  };

  // Generate download filename
  const generateFilename = (documentKey: string, sectionId: string, type: string) => {
    // Sanitize document key by replacing forward slashes with underscores
    const sanitizedDocId = documentKey.replace(/\//g, '_');
    return `${sanitizedDocId}_section${sectionId}_${type}.json`;
  };

  // Download handler for both prediction and baseline data
  const handleDownload = async (type: string) => {
    setIsDownloading(true);

    try {
      const fileUri = type === 'baseline' ? constructBaselineUri(item.OutputJSONUri) : item.OutputJSONUri;

      if (!fileUri) {
        alert('File URI not available');
        return;
      }

      logger.info(`Downloading ${type} data from:`, fileUri);

      // Resolve a presigned GET URL and fetch the bytes directly from S3.
      // Section result.json files can exceed Lambda's 6 MB synchronous
      // response cap, so we must not proxy the content through the resolver.
      const response = await client.graphql({
        query: getFilePresignedUrl,
        variables: { s3Uri: fileUri },
      });

      const result = response.data.getFilePresignedUrl;

      if (!result?.presignedUrl) {
        throw new Error('No presigned URL returned');
      }

      const s3Response = await fetch(result.presignedUrl);
      if (!s3Response.ok) {
        throw new Error(`S3 fetch failed: ${s3Response.status} ${s3Response.statusText}`);
      }
      const content = await s3Response.text();

      // Create blob and download
      const blob = new Blob([content], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');

      // Generate filename
      const documentKey = documentItem?.objectKey || documentItem?.ObjectKey || 'document';
      const filename = generateFilename(documentKey, item.Id, type);

      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      logger.info(`Successfully downloaded ${type} data as ${filename}`);
    } catch (error) {
      logger.error(`Error downloading ${type} data:`, error);

      let errorMessage = `Failed to download ${type} data`;

      if (type === 'baseline' && (error as Error).message?.includes('not found')) {
        errorMessage = 'Baseline data not found. The baseline may not have been set for this document yet.';
      } else if ((error as Error).message) {
        errorMessage = `Failed to download ${type} data: ${(error as Error).message}`;
      }

      alert(errorMessage);
    } finally {
      setIsDownloading(false);
    }
  };

  // Build dropdown menu items
  const downloadMenuItems: ButtonDropdownProps.ItemOrGroup[] = [
    {
      id: 'prediction',
      text: 'Download Data',
      iconName: 'download',
    },
  ];

  // Add baseline option if available
  if (isBaselineAvailable) {
    downloadMenuItems.push({
      id: 'baseline',
      text: 'Download Baseline',
      iconName: 'download',
    });
  }

  return (
    <SpaceBetween direction="horizontal" size="xs">
      <FileViewer
        fileUri={item.OutputJSONUri ?? ''}
        fileType="json"
        buttonText={isEditModeEnabled ? 'Edit Data' : 'View Data'}
        sectionData={{ ...item, pages, documentItem, mergedConfig, isSectionCompleted, isReviewerOnly }}
        onOpen={onViewerOpen}
        onClose={onViewerClose}
        disabled={shouldDisableViewEdit}
        isReadOnly={!isEditModeEnabled}
        allSections={allSections}
        currentSectionIndex={currentSectionIndex}
        onNavigateToSection={onNavigateToSection}
        isExternallyOpen={isViewerOpen}
      />
      {!isViewerOpen && (
        <ButtonDropdown
          items={downloadMenuItems}
          onItemClick={({ detail }) => handleDownload(detail.id)}
          disabled={isDownloading}
          loading={isDownloading}
          variant="normal"
          expandToViewport
        >
          Download
        </ButtonDropdown>
      )}
    </SpaceBetween>
  );
};

// Editable cell components for edit mode (moved outside render)
const EditableIdCell = ({
  item,
  validationErrors,
  updateSectionId,
}: {
  item: SectionItem;
  validationErrors: Record<string, string[]>;
  updateSectionId: (oldId: string, newId: string) => void;
}): React.JSX.Element => (
  <FormField errorText={validationErrors[item.Id]?.find((err) => err.includes('Section ID'))}>
    <Input
      value={item.Id}
      onChange={({ detail }) => updateSectionId(item.Id, detail.value)}
      placeholder="e.g., section_1"
      invalid={validationErrors[item.Id]?.some((err) => err.includes('Section ID'))}
    />
  </FormField>
);

const EditableClassCell = ({
  item,
  validationErrors,
  updateSection,
  getAvailableClasses,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
}: {
  item: SectionItem;
  validationErrors: Record<string, string[]>;
  updateSection: (id: string, field: string, value: string) => void;
  getAvailableClasses: () => { value: string; label: string; description?: string }[];
  classificationIndex?: ClassificationIndex;
}): React.JSX.Element => (
  <FormField errorText={validationErrors[item.Id]?.find((err) => err.includes('class'))}>
    <SpaceBetween size="xs">
      <Select
        selectedOption={getAvailableClasses().find((option) => option.value === item.Class) || null}
        onChange={({ detail }) => updateSection(item.Id, 'Class', detail.selectedOption.value ?? '')}
        options={getAvailableClasses()}
        placeholder="Select class/type"
        invalid={validationErrors[item.Id]?.some((err) => err.includes('class'))}
        filteringType="auto"
        expandToViewport
      />
      {/* Shown in edit mode too: this is where the class can actually be
          corrected, so knowing what ground truth expects is most actionable
          here. */}
      <SectionClassMismatch index={classificationIndex} pageNumbers={item.PageIds ?? []} predictedClass={item.Class} />
    </SpaceBetween>
  </FormField>
);

const EditablePageIdsCell = ({
  item,
  validationErrors,
  updateSection,
}: {
  item: SectionItem;
  validationErrors: Record<string, string[]>;
  updateSection: (id: string, field: string, value: number[]) => void;
}): React.JSX.Element => {
  // Store the raw input value separately from the parsed PageIds
  const [inputValue, setInputValue] = React.useState(item.PageIds && item.PageIds.length > 0 ? item.PageIds.join(', ') : '');

  // Update input value when item changes (e.g., when entering edit mode)
  React.useEffect(() => {
    setInputValue(item.PageIds && item.PageIds.length > 0 ? item.PageIds.join(', ') : '');
  }, [item.PageIds]);

  const parseAndUpdatePageIds = (value: string) => {
    const trimmedValue = value.trim();

    if (!trimmedValue) {
      updateSection(item.Id, 'PageIds', []);
      return;
    }

    // Parse comma-separated page IDs
    const rawPageIds = trimmedValue
      .split(/[,\s]+/) // Split on commas and/or whitespace
      .map((id: string) => id.trim())
      .filter((id: string) => id !== '');

    const seenIds = new Set<number>();

    const pageIds = rawPageIds
      .map((rawId: string) => parseInt(rawId, 10))
      .filter((parsed: number) => !Number.isNaN(parsed) && parsed > 0)
      .filter((parsed: number) => {
        if (seenIds.has(parsed)) {
          return false;
        }
        seenIds.add(parsed);
        return true;
      });

    updateSection(item.Id, 'PageIds', pageIds);
  };

  const handleInputChange = ({ detail }: { detail: { value: string } }) => {
    // Only update the input value, don't parse yet
    setInputValue(detail.value);
  };

  const handleBlur = () => {
    // Parse and update PageIds when user finishes editing
    parseAndUpdatePageIds(inputValue);
  };

  return (
    <FormField
      errorText={validationErrors[item.Id]?.find((err) => err.includes('Page') || err.includes('page'))}
      description="Enter page numbers separated by commas (e.g., 1, 2, 3)"
    >
      <Textarea
        value={inputValue}
        onChange={handleInputChange}
        onBlur={handleBlur}
        placeholder="1, 2, 3"
        {...({ autoComplete: 'off', spellCheck: false } as Record<string, unknown>)}
        rows={1}
        invalid={validationErrors[item.Id]?.some((err: string) => err.includes('Page') || err.includes('page'))}
      />
    </FormField>
  );
};

const EditableActionsCell = ({
  item,
  deleteSection,
  pages,
  documentItem,
  mergedConfig,
  // Navigation props for edit mode
  allSections = [],
  currentSectionIndex = 0,
  onNavigateToSection,
  onViewerOpen,
  onViewerClose,
  isViewerOpen = false,
}: {
  item: SectionItem;
  deleteSection: (id: string) => void;
  pages: PageItem[];
  documentItem: DocumentItem | undefined;
  mergedConfig: Record<string, unknown> | null | undefined;
  allSections?: SectionItem[];
  currentSectionIndex?: number;
  onNavigateToSection: (index: number) => void;
  onViewerOpen: () => void;
  onViewerClose: () => void;
  isViewerOpen?: boolean;
}) => {
  return (
    <SpaceBetween direction="horizontal" size="xs">
      <FileViewer
        fileUri={item.OutputJSONUri ?? ''}
        fileType="json"
        buttonText="Edit Data"
        sectionData={{ ...item, pages, documentItem, mergedConfig, isSectionCompleted: false, isReviewerOnly: false }}
        onOpen={onViewerOpen}
        onClose={onViewerClose}
        disabled={!item.OutputJSONUri}
        isReadOnly={false}
        allSections={allSections}
        currentSectionIndex={currentSectionIndex}
        onNavigateToSection={onNavigateToSection}
        isExternallyOpen={isViewerOpen}
      />
      {!isViewerOpen && <Button variant="icon" iconName="remove" ariaLabel="Delete section" onClick={() => deleteSection(item.Id)} />}
    </SpaceBetween>
  );
};

// Column definitions - now a factory that takes navigation params
const createColumnDefinitions = (
  pages: PageItem[],
  documentItem: DocumentItem | undefined,
  mergedConfig: Record<string, unknown> | null | undefined,
  isReviewerOnly: boolean,
  isEditModeEnabled: boolean,
  // Navigation params
  allSections: SectionItem[],
  openViewerSectionIndex: number | null,
  setOpenViewerSectionIndex: (index: number | null) => void,
  onNavigateToSection: (index: number) => void,
  classificationIndex: ClassificationIndex,
) => {
  // Get completed sections from documentItem
  const completedSections = documentItem?.hitlSectionsCompleted || [];

  return [
    {
      id: 'id',
      header: 'Section ID',
      cell: (item: SectionItem) => <IdCell item={item} />,
      sortingField: 'Id',
      minWidth: 160,
      width: 160,
      isResizable: true,
    },
    {
      id: 'class',
      header: 'Class/Type',
      cell: (item: SectionItem) => <ClassCell item={item} classificationIndex={classificationIndex} />,
      sortingField: 'Class',
      minWidth: 200,
      width: 200,
      isResizable: true,
    },
    {
      id: 'pageIds',
      header: 'Page IDs',
      cell: (item: SectionItem) => <PageIdsCell item={item} />,
      minWidth: 120,
      width: 120,
      isResizable: true,
    },
    {
      id: 'confidenceAlerts',
      header: 'Low-conf. fields',
      cell: (item: SectionItem) => <ConfidenceAlertsCell item={item} mergedConfig={mergedConfig} />,
      minWidth: 140,
      width: 140,
      isResizable: true,
    },
    {
      id: 'status',
      header: 'Status',
      cell: (item: SectionItem) => <StatusCell item={item} />,
      minWidth: 150,
      width: 150,
      isResizable: true,
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: (item: SectionItem) => {
        // Find index of current item in allSections
        const currentIndex = allSections?.findIndex((s: SectionItem) => s.Id === item.Id) ?? -1;
        const isThisViewerOpen = openViewerSectionIndex === currentIndex;

        return (
          <ActionsCell
            item={item}
            pages={pages}
            documentItem={documentItem}
            mergedConfig={mergedConfig}
            isSectionCompleted={completedSections.includes(item.Id)}
            isReviewerOnly={isReviewerOnly}
            isEditModeEnabled={isEditModeEnabled}
            allSections={allSections}
            currentSectionIndex={currentIndex}
            onNavigateToSection={onNavigateToSection}
            onViewerOpen={() => setOpenViewerSectionIndex(currentIndex)}
            onViewerClose={() => setOpenViewerSectionIndex(null)}
            isViewerOpen={isThisViewerOpen}
          />
        );
      },
      minWidth: 400,
      width: 400,
      isResizable: true,
    },
  ];
};

// Pattern-1 edit mode column definitions - data-only editing (read-only section structure)
const createPattern1EditColumnDefinitions = (
  pages: PageItem[],
  documentItem: DocumentItem | undefined,
  mergedConfig: Record<string, unknown> | null | undefined,
  // Navigation params
  allSections: SectionItem[],
  openViewerSectionIndex: number | null,
  setOpenViewerSectionIndex: (index: number | null) => void,
  onNavigateToSection: (index: number) => void,
  classificationIndex: ClassificationIndex,
) => {
  // Get completed sections from documentItem
  const completedSections = documentItem?.hitlSectionsCompleted || [];

  return [
    {
      id: 'id',
      header: 'Section ID',
      cell: (item: SectionItem) => <IdCell item={item} />,
      sortingField: 'Id',
      minWidth: 160,
      width: 160,
      isResizable: true,
    },
    {
      id: 'class',
      header: 'Class/Type',
      cell: (item: SectionItem) => <ClassCell item={item} classificationIndex={classificationIndex} />,
      sortingField: 'Class',
      minWidth: 200,
      width: 200,
      isResizable: true,
    },
    {
      id: 'pageIds',
      header: 'Page IDs',
      cell: (item: SectionItem) => <PageIdsCell item={item} />,
      minWidth: 120,
      width: 120,
      isResizable: true,
    },
    {
      id: 'confidenceAlerts',
      header: 'Low-conf. fields',
      cell: (item: SectionItem) => <ConfidenceAlertsCell item={item} mergedConfig={mergedConfig} />,
      minWidth: 140,
      width: 140,
      isResizable: true,
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: (item: SectionItem) => {
        // Find index of current item in allSections
        const currentIndex = allSections?.findIndex((s: SectionItem) => s.Id === item.Id) ?? -1;
        const isThisViewerOpen = openViewerSectionIndex === currentIndex;

        return (
          <SpaceBetween direction="horizontal" size="xs">
            <FileViewer
              fileUri={item.OutputJSONUri ?? ''}
              fileType="json"
              buttonText="Edit Data"
              sectionData={{
                ...item,
                pages,
                documentItem,
                mergedConfig,
                isSectionCompleted: completedSections.includes(item.Id),
                isReviewerOnly: false,
              }}
              onOpen={() => setOpenViewerSectionIndex(currentIndex)}
              onClose={() => setOpenViewerSectionIndex(null)}
              disabled={!item.OutputJSONUri}
              isReadOnly={false}
              allSections={allSections}
              currentSectionIndex={currentIndex}
              onNavigateToSection={onNavigateToSection}
              isExternallyOpen={isThisViewerOpen}
            />
          </SpaceBetween>
        );
      },
      minWidth: 200,
      width: 200,
      isResizable: true,
    },
  ];
};

// Edit mode column definitions for Pattern-2/3 - expanded to use maximum available width
const createEditColumnDefinitions = (
  validationErrors: Record<string, string[]>,
  updateSection: (id: string, field: string, value: unknown) => void,
  updateSectionId: (oldId: string, newId: string) => void,
  getAvailableClasses: () => { value: string; label: string }[],
  deleteSection: (id: string) => void,
  pages: PageItem[],
  documentItem: DocumentItem | undefined,
  mergedConfig: Record<string, unknown> | null | undefined,
  // Navigation params
  allSections: SectionItem[],
  openViewerSectionIndex: number | null,
  setOpenViewerSectionIndex: (index: number | null) => void,
  onNavigateToSection: (index: number) => void,
  classificationIndex: ClassificationIndex,
) => [
  {
    id: 'id',
    header: 'Section ID',
    cell: (item: SectionItem) => <EditableIdCell item={item} validationErrors={validationErrors} updateSectionId={updateSectionId} />,
    minWidth: 160,
    width: 300,
    isResizable: true,
  },
  {
    id: 'class',
    header: 'Class/Type',
    cell: (item: SectionItem) => (
      <EditableClassCell
        item={item}
        validationErrors={validationErrors}
        updateSection={updateSection}
        getAvailableClasses={getAvailableClasses}
        classificationIndex={classificationIndex}
      />
    ),
    minWidth: 200,
    width: 400,
    isResizable: true,
  },
  {
    id: 'pageIds',
    header: 'Page IDs',
    cell: (item: SectionItem) => <EditablePageIdsCell item={item} validationErrors={validationErrors} updateSection={updateSection} />,
    minWidth: 250,
    width: 500,
    isResizable: true,
  },
  {
    id: 'actions',
    header: 'Actions',
    cell: (item: SectionItem) => {
      // Find index of current item in allSections
      const currentIndex = allSections?.findIndex((s: SectionItem) => s.Id === item.Id) ?? -1;
      const isThisViewerOpen = openViewerSectionIndex === currentIndex;

      return (
        <EditableActionsCell
          item={item}
          deleteSection={deleteSection}
          pages={pages}
          documentItem={documentItem}
          mergedConfig={mergedConfig}
          allSections={allSections}
          currentSectionIndex={currentIndex}
          onNavigateToSection={onNavigateToSection}
          onViewerOpen={() => setOpenViewerSectionIndex(currentIndex)}
          onViewerClose={() => setOpenViewerSectionIndex(null)}
          isViewerOpen={isThisViewerOpen}
        />
      );
    },
    minWidth: 300,
    width: 350,
    isResizable: true,
  },
];

const SectionsPanel = ({
  sections,
  pages = [],
  documentItem,
  mergedConfig,
  onDocumentUpdate,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
}: SectionsPanelProps): React.JSX.Element => {
  const [isEditMode, setIsEditMode] = useState(false);
  const [isRegrouping, setIsRegrouping] = useState(false);
  const [isSavingGrouping, setIsSavingGrouping] = useState(false);
  const [groupingNotice, setGroupingNotice] = useState<string | null>(null);
  const thumbnailUrls = usePageThumbnails(pages);
  const [editedSections, setEditedSections] = useState<SectionItem[]>([]);
  const [validationErrors, setValidationErrors] = useState<Record<string, string[]>>({});
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [showSkipAllModal, setShowSkipAllModal] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [isSkipping, setIsSkipping] = useState(false);
  // Track which section's viewer is open for navigation
  const [openViewerSectionIndex, setOpenViewerSectionIndex] = useState<number | null>(null);
  // `mergedConfig` is the config VERSION the document was processed with
  // (see DocumentPanel: it fetches `documentVersionConfig` from the doc's
  // `configVersion` and passes it in here). Using the current live config
  // instead would show the wrong class vocabulary in Edit Mode for docs
  // processed under a previous or different configuration profile.
  const configuration = mergedConfig;
  const { settings: settings2 } = useSettingsContext();
  const { isReviewerOnly, canWrite, canReview } = useUserRole();
  // When viewing a past document version, all edits are disabled — the panels
  // write to the *current* output objects, not the historical snapshot.
  const { isHistorical } = useDocumentVersion();

  // Check if current pattern is Pattern-1 (for data-only edit mode)
  const isPattern1 = () => {
    const pattern = settings2?.IDPPattern as string | undefined;
    return pattern && pattern.toLowerCase().includes('pattern1');
  };

  // Check if document has pending HITL review
  const hitlStatusLower = documentItem?.hitlStatus?.toLowerCase().replace(/\s+/g, '') || '';
  const isHitlSkipped = hitlStatusLower === 'skipped' || hitlStatusLower === 'reviewskipped';
  const isHitlCompleted = hitlStatusLower === 'completed' || hitlStatusLower === 'reviewcompleted';
  const hasPendingHITL = documentItem?.hitlTriggered && !isHitlCompleted && !isHitlSkipped;
  // Show skip button only if HITL pending and not already completed/skipped
  // (never while viewing a historical version — it mutates current state).
  const showSkipAllButton = canReview && hasPendingHITL && !isHistorical;

  // Log for debugging
  logger.debug('HITL Status Check:', {
    hitlStatus: documentItem?.hitlStatus,
    hitlStatusLower,
    isHitlSkipped,
    isHitlCompleted,
    hitlTriggered: documentItem?.hitlTriggered,
    hasPendingHITL,
    showSkipAllButton,
  });

  // Edit Mode should be disabled for reviewers until they click Start Review (claim the document)
  const hasReviewOwner = !!(documentItem?.hitlReviewOwner || documentItem?.hitlReviewOwnerEmail);
  const hitlTriggered = !!documentItem?.hitlTriggered;

  // Check if document is currently processing (disable edit during reprocessing)
  const processingStatuses = ['queued', 'running', 'processing', 'postprocessing', 'summarizing', 'evaluating'];
  const docStatus = documentItem?.objectStatus?.toLowerCase() || '';
  const isDocumentProcessing = processingStatuses.includes(docStatus);

  /**
   * Pages for the board, in the document's OWN numbering.
   *
   * Passed through unconverted, unlike the test-set path: document page ids are 1-based
   * except BDA / Pattern-1, which is 0-based, and `section-grouping` is deliberately
   * base-agnostic for exactly this reason. Converting here would put an off-by-one into
   * the surface that has two numbering conventions.
   */
  const boardPages = useMemo(
    () => (pages ?? []).map((page) => ({ id: page.Id, imageUri: thumbnailUrls[String(page.Id)] ?? null })),
    [pages, thumbnailUrls],
  );

  const boardSections = useMemo<GroupedSection[]>(
    () =>
      (sections ?? []).map((section) => ({
        sectionId: String(section.Id),
        documentClass: section.Class ?? null,
        pageIds: (section.PageIds ?? []).map((id) => Number(id)),
      })),
    [sections],
  );

  const handleSaveGrouping = async (next: GroupedSection[]) => {
    const documentKey = documentItem?.objectKey || documentItem?.ObjectKey;
    if (!documentKey) return;
    setIsSavingGrouping(true);
    try {
      const response = await client.graphql({
        query: updateDocumentSections,
        variables: {
          objectKey: documentKey,
          sections: next.map((section) => ({
            sectionId: section.sectionId,
            classification: section.documentClass ?? undefined,
            pageIds: section.pageIds.map((id) => String(id)),
          })),
        },
      });
      const result = response.data?.updateDocumentSections;
      if (!result?.success) {
        // Surfaced rather than thrown: the resolver returns a reasoned refusal for the
        // cases a reviewer can act on — a document mid-pipeline, most of all.
        setGroupingNotice(result?.message ?? 'The page grouping could not be saved.');
        return;
      }
      setGroupingNotice(result.message ?? null);
      setIsRegrouping(false);
      // The document record changed underneath the page, so the caller refetches.
      if (onDocumentUpdate) onDocumentUpdate((prev) => ({ ...prev }));
    } catch (err) {
      logger.error('Could not save the page grouping:', err);
      setGroupingNotice(`Could not save the page grouping: ${getErrorMessage(err)}`);
    } finally {
      setIsSavingGrouping(false);
    }
  };

  // Disable edit mode if:
  // - User has no write or review permissions (Viewer role), OR
  // - REVIEWER only: HITL triggered but not claimed, document processing, or HITL completed/skipped
  // Admins and Authors can always edit
  // - Viewing a historical version (read-only snapshot)
  const isEditModeDisabled =
    isHistorical ||
    (!canWrite && !canReview) ||
    (isReviewerOnly && ((hitlTriggered && !hasReviewOwner) || isDocumentProcessing || isHitlCompleted || isHitlSkipped));

  logger.debug('Edit Mode Check:', { isReviewerOnly, isEditModeDisabled, isHitlCompleted, isHitlSkipped });

  // Auto-exit edit mode when switching to a historical version, or (for
  // reviewers) when the document starts processing or HITL is completed/skipped.
  useEffect(() => {
    if (isHistorical && isEditMode) {
      setIsEditMode(false);
      return;
    }
    if (isReviewerOnly && isEditMode && (isDocumentProcessing || isHitlCompleted || isHitlSkipped)) {
      logger.info('Auto-exiting edit mode due to status change');
      setIsEditMode(false);
    }
  }, [isHistorical, isReviewerOnly, isDocumentProcessing, isHitlCompleted, isHitlSkipped, isEditMode]);

  // Handle skip all sections review (Admin only)
  const handleSkipAllSections = async () => {
    setIsSkipping(true);
    setShowSkipAllModal(false);

    try {
      const objectKey = documentItem?.objectKey || documentItem?.ObjectKey;
      if (!objectKey) {
        throw new Error('Document object key is missing');
      }

      const result = await client.graphql({
        query: skipAllSectionsReview,
        variables: { objectKey },
      });

      logger.info('All sections review skipped successfully', result);

      // Update document state immediately with mutation response
      const updatedData = result.data.skipAllSectionsReview;
      if (updatedData && onDocumentUpdate) {
        // Parse HITLReviewHistory from AWSJSON using typed parser
        const reviewHistory = parseHITLReviewHistory(updatedData.HITLReviewHistory as string);

        onDocumentUpdate((prev) => {
          const newState = {
            ...prev,
            objectStatus: updatedData.ObjectStatus ?? prev.objectStatus,
            hitlStatus: updatedData.HITLStatus ?? prev.hitlStatus,
            hitlTriggered: updatedData.HITLTriggered ?? prev.hitlTriggered,
            hitlCompleted: updatedData.HITLCompleted ?? true,
            hitlSectionsPending: updatedData.HITLSectionsPending ?? [],
            hitlSectionsCompleted: updatedData.HITLSectionsCompleted ?? prev.hitlSectionsCompleted,
            hitlSectionsSkipped: updatedData.HITLSectionsSkipped ?? [],
            hitlReviewOwner: updatedData.HITLReviewOwner || prev.hitlReviewOwner,
            hitlReviewOwnerEmail: updatedData.HITLReviewOwnerEmail || prev.hitlReviewOwnerEmail,
            hitlReviewHistory: reviewHistory ?? prev.hitlReviewHistory,
          };
          logger.info('Skip All Reviews - Updated document state:', {
            hitlStatus: newState.hitlStatus,
            hitlCompleted: newState.hitlCompleted,
          });
          return newState;
        });
      }
    } catch (error) {
      logger.error('Failed to skip all sections review:', error);
      alert(`Failed to skip all sections: ${(error as Error).message || 'Unknown error'}`);
    } finally {
      setIsSkipping(false);
    }
  };

  // Initialize edited sections when entering edit mode
  useEffect(() => {
    if (isEditMode && sections) {
      const sectionsWithEditableFormat = sections.map((section) => ({
        ...section, // Copy all properties including OutputJSONUri
        Id: section.Id,
        Class: section.Class,
        PageIds: section.PageIds ? [...section.PageIds] : [],
        OriginalId: section.Id,
        isModified: false,
        isNew: false,
      }));
      setEditedSections(sectionsWithEditableFormat);
    }
  }, [isEditMode, sections]);

  // Get available classes from the document's configuration profile (passed in as
  // `mergedConfig`). Shared with Test Studio's annotation editor, which offers
  // the same correction against the same config.
  const getAvailableClasses = () => getConfigClassOptions(configuration);

  // Generate next sequential section ID
  const getNextSectionId = () => {
    const allSections = [...(sections || []), ...editedSections];

    // Extract all numeric values from existing section IDs
    const sectionNumbers = allSections
      .map((section) => {
        // Handle both formats: simple numbers ("1", "2") and prefixed ("section_1", "section_2")
        const simpleMatch = section.Id.match(/^\d+$/);
        const prefixedMatch = section.Id.match(/^section_(\d+)$/);

        if (simpleMatch) {
          return parseInt(section.Id, 10);
        }
        if (prefixedMatch) {
          return parseInt(prefixedMatch[1], 10);
        }
        return null;
      })
      .filter((num): num is number => num !== null && !Number.isNaN(num));

    // Determine the format to use based on existing sections
    const hasSimpleFormat = allSections.some((section) => /^\d+$/.test(section.Id));
    const hasPrefixedFormat = allSections.some((section) => /^section_\d+$/.test(section.Id));

    // Get the next number
    const maxNumber = sectionNumbers.length > 0 ? Math.max(...sectionNumbers) : 0;
    const nextNumber = maxNumber + 1;

    // Use existing format or default to simple format
    if (hasSimpleFormat && !hasPrefixedFormat) {
      return nextNumber.toString();
    }
    return `section_${nextNumber}`;
  };

  // Validate page ID overlaps and section ID uniqueness
  const validateSections = (sectionsToValidate: SectionItem[]): boolean => {
    const errors: Record<string, string[]> = {};
    const pageIdMap = new Map();
    const sectionIdMap = new Map();

    // Get available page IDs from the document
    const availablePageIds = pages ? pages.map((page) => page.Id) : [];
    const maxPageId = availablePageIds.length > 0 ? Math.max(...availablePageIds) : 0;

    sectionsToValidate.forEach((section) => {
      const sectionErrors = [];

      // Check for empty or invalid section ID
      if (!section.Id || !section.Id.trim()) {
        sectionErrors.push('Section ID cannot be empty');
      } else if (sectionIdMap.has(section.Id)) {
        sectionErrors.push(`Section ID '${section.Id}' is already used by another section`);
      } else {
        sectionIdMap.set(section.Id, true);
      }

      // Check for empty page IDs
      if (!section.PageIds || section.PageIds.length === 0) {
        sectionErrors.push('Section must have at least one valid page ID');
      } else {
        // Check each page ID for validity
        const invalidPageIds: number[] = [];
        const nonExistentPageIds: number[] = [];

        section.PageIds.forEach((pageId) => {
          // Check if page ID is valid (should be handled by parsing, but double-check)
          // Note: BDA (Pattern-1) uses 0-based page indices, so we allow pageId >= 0
          if (!Number.isInteger(pageId) || pageId < 0) {
            invalidPageIds.push(pageId);
          } else if (!availablePageIds.includes(pageId)) {
            // Check if page exists in document
            nonExistentPageIds.push(pageId);
          } else if (pageIdMap.has(pageId)) {
            // Check for overlaps with other sections
            const conflictSection = pageIdMap.get(pageId);
            sectionErrors.push(`Page ${pageId} is already assigned to section ${conflictSection}`);
          } else {
            pageIdMap.set(pageId, section.Id);
          }
        });

        // Add specific error messages for invalid page IDs
        if (invalidPageIds.length > 0) {
          sectionErrors.push(`Invalid page IDs: ${invalidPageIds.join(', ')} (must be non-negative integers)`);
        }

        if (nonExistentPageIds.length > 0) {
          const minPageId = availablePageIds.length > 0 ? Math.min(...availablePageIds) : 0;
          sectionErrors.push(
            `Page IDs ${nonExistentPageIds.join(', ')} do not exist in this document (available: ${minPageId}-${maxPageId})`,
          );
        }
      }

      if (sectionErrors.length > 0) {
        errors[section.Id] = sectionErrors;
      }
    });

    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  };

  // Handle section modifications
  const updateSection = (sectionId: string, field: string, value: unknown): void => {
    const updatedSections = editedSections.map((section) => {
      if (section.Id === sectionId) {
        const updated = {
          ...section,
          [field]: value,
          isModified: true,
        };
        return updated;
      }
      return section;
    });

    setEditedSections(updatedSections);

    // Re-validate after changes
    setTimeout(() => validateSections(updatedSections), 0);
  };

  // Handle section ID updates
  const updateSectionId = (oldId: string, newId: string): void => {
    const updatedSections = editedSections.map((section) => {
      if (section.Id === oldId) {
        return {
          ...section,
          Id: newId.trim(),
          isModified: true,
        };
      }
      return section;
    });

    setEditedSections(updatedSections);

    // Update validation errors - move errors from old ID to new ID
    const updatedErrors = { ...validationErrors };
    if (updatedErrors[oldId]) {
      updatedErrors[newId.trim()] = updatedErrors[oldId];
      delete updatedErrors[oldId];
      setValidationErrors(updatedErrors);
    }

    // Re-validate after changes
    setTimeout(() => validateSections(updatedSections), 0);
  };

  // Add new section
  const addSection = () => {
    const newId = getNextSectionId();
    const newSection: SectionItem = {
      Id: newId,
      Class: '',
      PageIds: [] as number[],
      OriginalId: null,
      isModified: false,
      isNew: true,
    };

    const updatedSections = [...editedSections, newSection];
    setEditedSections(updatedSections);
  };

  // Delete section
  const deleteSection = (sectionId: string): void => {
    const updatedSections = editedSections.filter((section) => section.Id !== sectionId);
    setEditedSections(updatedSections);

    // Remove validation errors for deleted section
    const updatedErrors = { ...validationErrors };
    delete updatedErrors[sectionId];
    setValidationErrors(updatedErrors);

    // Re-validate remaining sections
    setTimeout(() => validateSections(updatedSections), 0);
  };

  // Sort sections by starting page ID
  const sortSectionsByPageId = (sectionsToSort: SectionItem[]) => {
    return [...sectionsToSort].sort((a, b) => {
      const aMin = Math.min(...(a.PageIds || [Infinity]));
      const bMin = Math.min(...(b.PageIds || [Infinity]));
      return aMin - bMin;
    });
  };

  // Check if a section has actually been modified
  const hasActualChanges = (section: SectionItem, originalSections: SectionItem[] | undefined) => {
    // If it's a new section, it's always a change
    if (section.isNew) {
      return true;
    }

    // Find the original section
    const originalSection = originalSections?.find((orig: SectionItem) => orig.Id === section.OriginalId);
    if (!originalSection) {
      // If we can't find the original, treat as modified (shouldn't happen)
      return true;
    }

    // Check for changes in classification
    if (section.Class !== originalSection.Class) {
      return true;
    }

    // Page ids, compared in order. Both sides used to be sorted before comparing, which
    // made a pure reorder invisible — Save would stay disabled on a real change — and the
    // sort had no comparator, so it ordered numbers lexicographically (1, 10, 2). Order is
    // part of the grouping: it is what `split_accuracy_with_order` scores.
    const originalPageIds = originalSection.PageIds || [];
    const currentPageIds = section.PageIds || [];

    if (originalPageIds.length !== currentPageIds.length) {
      return true;
    }

    for (let i = 0; i < originalPageIds.length; i += 1) {
      if (originalPageIds[i] !== currentPageIds[i]) {
        return true;
      }
    }

    // Check for section ID changes
    if (section.Id !== section.OriginalId) {
      return true;
    }

    return false;
  };

  // Handle Edit Sections button click
  // For Pattern-1: enters "data-only" edit mode (can edit data but not section structure)
  // For Pattern-2/3: enters full edit mode (can edit data, section structure, add/delete sections)
  const handleEditSectionsClick = () => {
    setIsEditMode(true);
  };

  // Handle save changes
  const handleSaveChanges = async () => {
    if (!validateSections(editedSections)) {
      return;
    }

    setShowConfirmModal(true);
  };

  // Confirm and process changes
  const confirmSaveChanges = async () => {
    setIsProcessing(true);
    setShowConfirmModal(false);

    try {
      // Try different possible property names for the object key
      const objectKey =
        documentItem?.ObjectKey ||
        documentItem?.objectKey ||
        documentItem?.key ||
        documentItem?.Key ||
        documentItem?.id ||
        documentItem?.Id;

      if (!objectKey) {
        const availableProps = documentItem ? Object.keys(documentItem).join(', ') : 'none';
        throw new Error(`Document object key is missing. Available properties: ${availableProps}`);
      }

      // Filter to only include sections that have actually changed
      const actuallyModifiedSections = editedSections.filter((section) => hasActualChanges(section, sections));

      // Sort modified sections by starting page ID
      const sortedModifiedSections = sortSectionsByPageId(actuallyModifiedSections);

      // Create payload for actually modified sections only
      const modifiedSections = sortedModifiedSections.map((section) => ({
        sectionId: section.Id,
        classification: section.Class,
        pageIds: section.PageIds,
        isNew: section.isNew,
        isDeleted: false,
      }));

      // Find deleted sections
      const deletedSectionIds =
        sections
          ?.filter((original) => !editedSections.find((edited) => edited.OriginalId === original.Id))
          ?.map((section) => ({
            sectionId: section.Id,
            classification: section.Class,
            pageIds: section.PageIds,
            isNew: false,
            isDeleted: true,
          })) || [];

      const allChanges = [...modifiedSections, ...deletedSectionIds];

      // Log the changes for debugging
      console.log(`Processing ${allChanges.length} actual changes out of ${editedSections.length} total sections`);
      console.log('Modified sections:', modifiedSections);
      console.log('Deleted sections:', deletedSectionIds);

      // Call the GraphQL API with timeout
      const result = await Promise.race([
        client.graphql({
          query: processChanges,
          variables: {
            objectKey,
            modifiedSections: allChanges,
          },
        }),
        new Promise<never>((_, reject) => {
          setTimeout(() => reject(new Error('Request timed out after 30 seconds')), 30000);
        }),
      ]);

      const response = result.data.processChanges;

      if (!response?.success) {
        throw new Error((response?.message as string) || 'Failed to process changes - no response received');
      }

      // Update document state with new HITL status (review completed via Save and Reprocess)
      if (onDocumentUpdate) {
        onDocumentUpdate((prev) => ({
          ...prev,
          hitlStatus: 'Review Completed',
          hitlCompleted: true,
          objectStatus: 'QUEUED',
        }));
      }

      // Exit edit mode
      setIsEditMode(false);
      setEditedSections([]);
      setValidationErrors({});

      alert('Section changes submitted!');
    } catch (error) {
      // Handle different types of errors
      let errorMessage = 'Failed to process changes';
      const err = error as { message?: string; errors?: { message?: string }[]; data?: { processChanges?: { message?: string } } };

      if (err?.message) {
        errorMessage = err.message;
      } else if (err?.errors && err.errors.length > 0) {
        errorMessage = err.errors[0].message || 'GraphQL error occurred';
      } else if (typeof error === 'string') {
        errorMessage = error;
      } else if (err?.data?.processChanges?.message) {
        errorMessage = err.data.processChanges.message;
      }

      alert(`Error processing changes: ${errorMessage}`);
    } finally {
      setIsProcessing(false);
    }
  };

  // Cancel edit mode
  const cancelEdit = () => {
    setIsEditMode(false);
    setEditedSections([]);
    setValidationErrors({});
  };

  // Handle section navigation - just update the open section index
  // The FileViewer will close current viewer and open the new one based on the new index
  const handleNavigateToSection = (newIndex: number): void => {
    logger.info('Section navigation requested:', { from: openViewerSectionIndex, to: newIndex });
    // Update the open viewer index - this triggers the FileViewer to close and re-open with new section
    setOpenViewerSectionIndex(newIndex);
  };

  // Get all sections for navigation (use current view - edited or original)
  const allSectionsForNav = isEditMode ? editedSections : sections || [];

  // Determine which columns and data to use
  // Pattern-1: uses data-only edit mode (no section structure editing)
  // Pattern-2/3: uses full edit mode with section structure editing
  const columnDefinitions = isEditMode
    ? isPattern1()
      ? createPattern1EditColumnDefinitions(
          pages,
          documentItem,
          mergedConfig,
          // Navigation params for edit mode
          allSectionsForNav,
          openViewerSectionIndex,
          setOpenViewerSectionIndex,
          handleNavigateToSection,
          classificationIndex,
        )
      : createEditColumnDefinitions(
          validationErrors,
          updateSection,
          updateSectionId,
          getAvailableClasses,
          deleteSection,
          pages,
          documentItem,
          mergedConfig,
          // Navigation params for edit mode
          allSectionsForNav,
          openViewerSectionIndex,
          setOpenViewerSectionIndex,
          handleNavigateToSection,
          classificationIndex,
        )
    : createColumnDefinitions(
        pages,
        documentItem,
        mergedConfig,
        isReviewerOnly,
        isEditMode,
        // Navigation params
        allSectionsForNav,
        openViewerSectionIndex,
        setOpenViewerSectionIndex,
        handleNavigateToSection,
        classificationIndex,
      );

  // Sort sections by their starting page ID for consistent display order.
  // During parallel Map state execution (Extraction/Assessment), subscription events
  // may arrive out of order — the DynamoDB Sections array order depends on which
  // parallel Lambda finishes first. Sorting ensures stable visual ordering.
  const tableItems = isEditMode ? editedSections : sortSectionsByPageId(sections || []);

  // Sorting via the design system's own collection hook rather than a hand-rolled
  // sort: it honours BOTH `sortingField` and `sortingComparator` columns and owns
  // the direction, so every column that declares a sort works. Starts unsorted,
  // so the default view keeps its existing order. `sortedItems` feeds the table
  // ONLY — `tableItems` still feeds everything else, which must stay in document
  // order however the table is sorted.
  const { items: sortedItems, collectionProps } = useCollection(tableItems, { sorting: {} });

  // Check if there are any validation errors
  const hasValidationErrors = Object.keys(validationErrors).length > 0;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                {!isEditMode ? (
                  <>
                    {showSkipAllButton && (
                      <Button variant="normal" onClick={() => setShowSkipAllModal(true)} disabled={isSkipping} loading={isSkipping}>
                        Skip All Reviews
                      </Button>
                    )}
                    {/* Distinct from Edit Mode on purpose, and the labels carry the
                        difference: this keeps the extracted values, whereas Edit Mode's
                        save is already called "Process Changes" / "Save and Reprocess"
                        because it regenerates them. Hidden for Pattern-1, where BDA owns
                        the section structure. */}
                    {!isPattern1() && (
                      <Button iconName="edit" onClick={() => setIsRegrouping(true)} disabled={isEditModeDisabled}>
                        Edit page grouping
                      </Button>
                    )}
                    <Button variant="primary" iconName="edit" onClick={handleEditSectionsClick} disabled={isEditModeDisabled}>
                      Edit Mode
                    </Button>
                  </>
                ) : (
                  <>
                    <Button variant="link" onClick={cancelEdit} disabled={isProcessing}>
                      Cancel
                    </Button>
                    {/* Hide Add Section button for Pattern-1 (section structure managed by BDA) */}
                    {!isPattern1() && (
                      <Button iconName="add-plus" onClick={addSection} disabled={isProcessing}>
                        Add Section
                      </Button>
                    )}
                    <Button
                      variant="primary"
                      iconName="external"
                      onClick={handleSaveChanges}
                      disabled={(hasValidationErrors && !isPattern1()) || isProcessing}
                      loading={isProcessing}
                    >
                      {isPattern1() ? 'Save and Reprocess' : 'Process Changes'}
                    </Button>
                  </>
                )}
              </SpaceBetween>
            }
          >
            Document Sections
          </Header>
        }
      >
        {groupingNotice && (
          <Alert type="info" dismissible onDismiss={() => setGroupingNotice(null)}>
            {groupingNotice}
          </Alert>
        )}

        {isRegrouping && (
          <PageGroupingEditor
            pages={boardPages}
            sections={boardSections}
            classOptions={getAvailableClasses()}
            canChangeClass={!isEditModeDisabled}
            consequence={
              <>
                Moving pages rewrites this document&apos;s <b>section grouping</b>. The extracted field values are <b>kept</b> — including
                any a reviewer corrected — and the document is <b>not</b> reprocessed, so they may no longer match their pages. Use{' '}
                <b>Edit Mode → Process Changes</b> afterwards if you would rather the pipeline redo them.
              </>
            }
            saveLabel="Save page grouping"
            isSaving={isSavingGrouping}
            onSave={handleSaveGrouping}
            onCancel={() => setIsRegrouping(false)}
          />
        )}

        {hasValidationErrors && (
          <Alert type="error" header="Validation Errors">
            Please fix the following errors before saving:
            <ul>
              {Object.entries(validationErrors).map(([sectionId, errors]) => (
                <li key={sectionId}>
                  <strong>Section {sectionId}:</strong>
                  <ul>
                    {errors.map((error) => (
                      <li key={`${sectionId}-error-${error.slice(0, 50)}`}>{error}</li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </Alert>
        )}

        <div style={{ overflowX: 'auto', position: 'relative' }}>
          <Table
            columnDefinitions={columnDefinitions}
            items={sortedItems}
            {...collectionProps}
            variant="embedded"
            resizableColumns
            stickyHeader={false}
            empty={
              <Box textAlign="center" color="inherit">
                <b>No sections</b>
                <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                  {isEditMode ? "Click 'Add Section' to create a new section." : 'This document has no sections.'}
                </Box>
              </Box>
            }
            wrapLines
          />
        </div>
      </Container>

      {/* Confirmation Modal */}
      <Modal
        onDismiss={() => setShowConfirmModal(false)}
        visible={showConfirmModal}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowConfirmModal(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={confirmSaveChanges}>
                Confirm & Process
              </Button>
            </SpaceBetween>
          </Box>
        }
        header="Confirm Reprocessing"
      >
        <SpaceBetween size="s">
          {(() => {
            // Calculate changes to determine modal content
            const actuallyModifiedSections = editedSections.filter((section) => hasActualChanges(section, sections));
            const deletedSectionIds =
              sections?.filter((original) => !editedSections.find((edited) => edited.OriginalId === original.Id)) || [];
            const hasStructuralChanges = actuallyModifiedSections.length > 0 || deletedSectionIds.length > 0;

            if (hasStructuralChanges) {
              return (
                <>
                  <Box>You are about to save changes to document sections and trigger selective reprocessing. This will:</Box>
                  <ul>
                    <li>Update section classifications and page assignments</li>
                    <li>Remove extraction data for modified sections</li>
                    <li>Reprocess only the changed sections (skipping OCR and classification steps)</li>
                  </ul>
                </>
              );
            }
            return (
              <>
                <Box>
                  No section structure changes detected. This will trigger <strong>evaluation and summarization</strong> reprocessing.
                </Box>
                <Box>Use this when you have edited extraction data (predictions or baseline) and want to:</Box>
                <ul>
                  <li>Re-run evaluation to compare predictions against baseline</li>
                  <li>Update the document summary report</li>
                </ul>
                <Alert type="info">
                  {isPattern1()
                    ? 'BDA (Bedrock Data Automation) processing will be automatically skipped since existing data is preserved.'
                    : 'OCR, Classification, Extraction, and Assessment steps will be automatically skipped since existing data is preserved.'}
                </Alert>
              </>
            );
          })()}
        </SpaceBetween>
      </Modal>

      {/* Skip All Sections Review Modal (Admin only) */}
      <Modal
        onDismiss={() => setShowSkipAllModal(false)}
        visible={showSkipAllModal}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowSkipAllModal(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleSkipAllSections}>
                Skip All Reviews
              </Button>
            </SpaceBetween>
          </Box>
        }
        header="Skip All Section Reviews"
      >
        <SpaceBetween size="s">
          <Alert type="warning">This action will skip all pending section reviews and continue the document processing workflow.</Alert>
          <Box>Skipping review will:</Box>
          <ul>
            <li>Mark all pending sections as review skipped without human verification</li>
            <li>Record this action in the review history</li>
          </ul>
          <Box>Are you sure you want to skip all section reviews?</Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default SectionsPanel;
