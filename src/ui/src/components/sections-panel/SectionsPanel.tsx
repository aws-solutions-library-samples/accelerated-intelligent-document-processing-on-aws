// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import {
  Box,
  Container,
  SpaceBetween,
  Table,
  StatusIndicator,
  ButtonDropdown,
  Header,
  Badge,
  Popover,
} from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import { getSectionConfidenceAlertCount, getSectionConfidenceAlerts } from '../common/confidence-alerts-utils';
import { SectionClassMismatch } from '../common/ClassMismatchIndicator';
import ClassNameText from '../common/ClassNameText';
import { EMPTY_CLASSIFICATION_INDEX, type ClassificationIndex } from '../common/classification-comparison-utils';
import { getSectionIssueStatus } from '../common/processing-issues-utils';
import type { EditableSection } from '../../types/documents';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import { documentEditHref } from '../../routes/constants';
import { getFilePresignedUrl } from '../../graphql/generated';
import { isBaselineAvailable } from '../document-panel/document-export';
import { SECTION_NO_DATA_REASON } from '../document-actions/action-reasons';

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
  documentItem?: DocumentItem;
  mergedConfig?: Record<string, unknown> | null;
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

  // Only rendered when the section holds more than one record, so a normal
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
// column of its own. `InstanceCount` is how many separate records of this
// section's Class extraction found in it, and it is worth screen space in
// exactly one case:
//   > 1        -> the section holds several distinct records that
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
// "how many records are in these pages" and the extra records were NOT
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
      header={suspected ? 'Records may be missing from this section' : 'Multiple records in one section'}
      content={
        <SpaceBetween size="xs">
          {suspected ? (
            <Box variant="p">
              These pages appear to contain {count} separate {item.Class ? `${item.Class} records` : 'records'}, but only the first was
              extracted — the rest are not in the result.
            </Box>
          ) : (
            <Box variant="p">
              Extraction found {count} separate {item.Class ? `${item.Class} records` : 'records'} in this section. Each one was extracted
              as its own instance.
            </Box>
          )}
          <Box variant="small" color="text-body-secondary">
            {suspected
              ? 'Split the section (classification section splitting), or turn on multi-instance extraction for this class so every record is extracted.'
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

/**
 * A section row's ⋮ menu: its data in the edit page's direct edit, on that section ("Edit
 * Data" for a user who may edit, Admin, Author or Reviewer; "View Data" otherwise; the
 * editor decides what is editable), and its data and baseline as JSON.
 */
const ActionsCell = ({ item, documentItem }: { item: SectionItem; documentItem: DocumentItem | undefined }) => {
  const [isDownloading, setIsDownloading] = React.useState(false);
  const { settings } = useSettingsContext();
  const { setErrorMessage } = useAppContext();
  const { canWrite, canReview } = useUserRole();
  const canEdit = canWrite || canReview;
  const objectKey = (documentItem?.objectKey || documentItem?.ObjectKey || '') as string;

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

    const [, bucketName, key] = match;

    // Verify this is actually the output bucket before replacing
    if (bucketName !== outputBucketName) {
      logger.warn(`URI bucket (${bucketName}) does not match expected output bucket (${outputBucketName})`);
    }

    // Replace the output bucket with the baseline bucket (same object key)
    const baselineUri = `s3://${baselineBucketName}/${key}`;

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
        setErrorMessage('File URI not available');
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

      setErrorMessage(errorMessage);
    } finally {
      setIsDownloading(false);
    }
  };

  const items: ButtonDropdownProps.ItemOrGroup[] = [
    {
      id: 'edit-data',
      text: canEdit ? 'Edit Data' : 'View Data',
      href: documentEditHref(objectKey, { section: String(item.Id) }),
      ...(canEdit ? { iconName: 'edit' as const } : {}),
      disabled: !objectKey || !item.OutputJSONUri,
      disabledReason: !item.OutputJSONUri ? SECTION_NO_DATA_REASON : undefined,
    },
    { id: 'prediction', text: 'Download Data', iconName: 'download' },
    ...(isBaselineAvailable(documentItem) ? [{ id: 'baseline', text: 'Download Baseline', iconName: 'download' as const }] : []),
  ];

  return (
    <ButtonDropdown
      items={items}
      variant="inline-icon"
      expandToViewport
      ariaLabel={`Actions for section ${item.Id}`}
      loading={isDownloading}
      onItemClick={({ detail }) => {
        if (detail.id === 'prediction' || detail.id === 'baseline') void handleDownload(detail.id);
      }}
    />
  );
};

/**
 * The sections table's columns. The data columns have no width, so they start even and
 * stay resizable; the ⋮ Actions column is last and narrow.
 */
export const sectionColumnDefinitions = (
  documentItem: DocumentItem | undefined,
  mergedConfig: Record<string, unknown> | null | undefined,
  classificationIndex: ClassificationIndex,
) => {
  return [
    {
      id: 'id',
      header: 'Section ID',
      cell: (item: SectionItem) => <IdCell item={item} />,
      sortingField: 'Id',
    },
    {
      id: 'class',
      header: 'Class/Type',
      cell: (item: SectionItem) => <ClassCell item={item} classificationIndex={classificationIndex} />,
      sortingField: 'Class',
    },
    {
      id: 'pageIds',
      header: 'Page IDs',
      cell: (item: SectionItem) => <PageIdsCell item={item} />,
    },
    {
      id: 'confidenceAlerts',
      header: 'Low-conf. fields',
      cell: (item: SectionItem) => <ConfidenceAlertsCell item={item} mergedConfig={mergedConfig} />,
    },
    {
      id: 'status',
      header: 'Status',
      cell: (item: SectionItem) => <StatusCell item={item} />,
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: (item: SectionItem) => <ActionsCell item={item} documentItem={documentItem} />,
      width: 60,
    },
  ];
};

const SectionsPanel = ({
  sections,
  documentItem,
  mergedConfig,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
}: SectionsPanelProps): React.JSX.Element => {
  // Sort sections by their starting page ID for consistent display order.
  // During parallel Map state execution (Extraction/Assessment), events may arrive out
  // of order — the DynamoDB Sections array order depends on which parallel Lambda
  // finishes first. Sorting ensures stable visual ordering.
  const tableItems = [...(sections || [])].sort((a, b) => {
    const aMin = Math.min(...(a.PageIds || [Infinity]));
    const bMin = Math.min(...(b.PageIds || [Infinity]));
    return aMin - bMin;
  });

  // Sorting via the design system's own collection hook rather than a hand-rolled
  // sort: it honours BOTH `sortingField` and `sortingComparator` columns and owns
  // the direction, so every column that declares a sort works. Starts unsorted,
  // so the default view keeps its existing order.
  const { items: sortedItems, collectionProps } = useCollection(tableItems, { sorting: {} });

  return (
    <Container header={<Header variant="h2">Document Sections</Header>}>
      <div style={{ overflowX: 'auto', position: 'relative' }}>
        <Table
          columnDefinitions={sectionColumnDefinitions(documentItem, mergedConfig, classificationIndex)}
          items={sortedItems}
          {...collectionProps}
          variant="embedded"
          resizableColumns
          stickyHeader={false}
          empty={
            <Box textAlign="center" color="inherit">
              <b>No sections</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                This document has no sections.
              </Box>
            </Box>
          }
          wrapLines
        />
      </div>
    </Container>
  );
};

export default SectionsPanel;
