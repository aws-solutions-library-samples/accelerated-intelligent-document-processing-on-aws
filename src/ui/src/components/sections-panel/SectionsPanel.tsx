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
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import { getSectionConfidenceAlertCount, getSectionConfidenceAlerts } from '../common/confidence-alerts-utils';
import { SectionClassMismatch } from '../common/ClassMismatchIndicator';
import ClassNameText from '../common/ClassNameText';
import { EMPTY_CLASSIFICATION_INDEX, type ClassificationIndex } from '../common/classification-comparison-utils';
import { getSectionIssueStatus, issueCodeLabel } from '../common/processing-issues-utils';
import { getSectionMissingRequiredFields } from '../common/missing-required-fields-utils';
import type { EditableSection } from '../../types/documents';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import { documentEditHref } from '../../routes/constants';
import { getFilePresignedUrl } from '../../graphql/generated';
import { isBaselineAvailable } from '../document-panel/document-export';
import { actionReasonMessages } from '../document-actions/action-reasons';
import { ColumnPreferences, fillTableColumns, useColumnPreferences } from '../common/detail-table-columns';

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

const messages = defineMessages<{
  title: NoMessageValues;
  skipped: { readonly reason: string };
  skippedNoReason: NoMessageValues;
  suspectedHeader: NoMessageValues;
  detectedHeader: NoMessageValues;
  suspectedBody: { readonly count: number; readonly className: string };
  suspectedBodyNoClass: { readonly count: number };
  detectedBody: { readonly count: number; readonly className: string };
  detectedBodyNoClass: { readonly count: number };
  suspectedHint: NoMessageValues;
  detectedHint: NoMessageValues;
  missingRequiredHeader: NoMessageValues;
  missingRequiredUnnamed: NoMessageValues;
  missingRequiredBody: NoMessageValues;
  issuesHeader: NoMessageValues;
  issueFallback: NoMessageValues;
  rootCause: { readonly rootCause: string };
  downloadNoFileUri: NoMessageValues;
  downloadPredictionFailed: NoMessageValues;
  downloadPredictionFailedWithError: { readonly error: string };
  downloadBaselineFailed: NoMessageValues;
  downloadBaselineFailedWithError: { readonly error: string };
  downloadBaselineNotFound: NoMessageValues;
  editData: NoMessageValues;
  viewData: NoMessageValues;
  downloadData: NoMessageValues;
  downloadBaseline: NoMessageValues;
  actionsAriaLabel: { readonly section: string };
  emptyTitle: NoMessageValues;
  emptyBody: NoMessageValues;
}>({
  title: {
    id: 'documents.sections.title',
    defaultMessage: 'Document Sections',
    description: 'Heading of the sections table on the document page; also names the table in its column preferences.',
  },
  skipped: {
    id: 'documents.sections.skipped',
    defaultMessage: 'Skipped: {reason}',
    description:
      "Badge beside a section's class in the Document Sections table when the configuration excludes that class from processing. {reason} is the exclusion reason the configuration gives, as written there.",
  },
  skippedNoReason: {
    id: 'documents.sections.skippedNoReason',
    defaultMessage: 'Skipped: excluded',
    description:
      "Badge beside a section's class in the Document Sections table when the configuration excludes that class from processing and gives no reason.",
  },
  suspectedHeader: {
    id: 'documents.sections.multiInstance.suspectedHeader',
    defaultMessage: 'Records may be missing from this section',
    description:
      "Heading of the popover behind a section's record count in the Document Sections table, when its pages appear to hold more records than were extracted.",
  },
  detectedHeader: {
    id: 'documents.sections.multiInstance.detectedHeader',
    defaultMessage: 'Multiple records in one section',
    description:
      "Heading of the popover behind a section's record count in the Document Sections table, when extraction found and extracted several records in the section.",
  },
  suspectedBody: {
    id: 'documents.sections.multiInstance.suspectedBody',
    defaultMessage:
      'These pages appear to contain {count} separate {className} records, but only the first was extracted — the rest are not in the result.',
    description:
      "Text of the popover behind a section's record count when records may be missing. {count} is the number of records the pages appear to contain (2 or more); {className} is the section's class.",
  },
  suspectedBodyNoClass: {
    id: 'documents.sections.multiInstance.suspectedBodyNoClass',
    defaultMessage:
      'These pages appear to contain {count} separate records, but only the first was extracted — the rest are not in the result.',
    description:
      "Text of the popover behind a section's record count when records may be missing, for a section without a class. {count} is the number of records the pages appear to contain (2 or more).",
  },
  detectedBody: {
    id: 'documents.sections.multiInstance.detectedBody',
    defaultMessage: 'Extraction found {count} separate {className} records in this section. Each one was extracted as its own instance.',
    description:
      "Text of the popover behind a section's record count when several records were extracted. {count} is the number of records (2 or more); {className} is the section's class.",
  },
  detectedBodyNoClass: {
    id: 'documents.sections.multiInstance.detectedBodyNoClass',
    defaultMessage: 'Extraction found {count} separate records in this section. Each one was extracted as its own instance.',
    description:
      "Text of the popover behind a section's record count when several records were extracted, for a section without a class. {count} is the number of records (2 or more).",
  },
  suspectedHint: {
    id: 'documents.sections.multiInstance.suspectedHint',
    defaultMessage:
      'Split the section (classification section splitting), or turn on multi-instance extraction for this class so every record is extracted.',
    description: "Advice under the text of the popover behind a section's record count, when records may be missing.",
  },
  detectedHint: {
    id: 'documents.sections.multiInstance.detectedHint',
    defaultMessage: 'If these should be separate sections, review the classification settings for this class.',
    description: "Advice under the text of the popover behind a section's record count, when several records were extracted.",
  },
  missingRequiredHeader: {
    id: 'documents.sections.missingRequired.header',
    defaultMessage: 'Missing required fields',
    description:
      'Heading of the popover behind a count in the Missing Required Fields column of the Document Sections table; it lists the required fields extraction found no value for.',
  },
  missingRequiredUnnamed: {
    id: 'documents.sections.missingRequired.unnamedField',
    defaultMessage: '(unnamed field)',
    description: 'Shown in the list of missing required fields in place of a field that has no name.',
  },
  missingRequiredBody: {
    id: 'documents.sections.missingRequired.body',
    defaultMessage: 'The class schema marks these fields required and extraction found no value for them. They raise no confidence alert.',
    description: 'Explanation under the list of missing required fields in their popover.',
  },
  issuesHeader: {
    id: 'documents.sections.issues.header',
    defaultMessage: 'Processing issues',
    description:
      "Heading of the popover behind a section's status in the Document Sections table; it lists the section's processing issues.",
  },
  issueFallback: {
    id: 'documents.sections.issues.fallback',
    defaultMessage: 'issue',
    description:
      "Shown in the Processing issues popover in place of an issue's code when the issue has neither a code nor a processing stage.",
  },
  rootCause: {
    id: 'documents.sections.issues.rootCause',
    defaultMessage: 'Root cause: {rootCause}',
    description:
      "Line under a processing issue's message in the Processing issues popover. {rootCause} is the cause the backend recorded, in English.",
  },
  downloadNoFileUri: {
    id: 'documents.sections.download.noFileUri',
    defaultMessage: 'File URI not available',
    description: "Error notice when a section row's Download Data or Download Baseline finds no file location to download from.",
  },
  downloadPredictionFailed: {
    id: 'documents.sections.download.predictionFailed',
    defaultMessage: 'Failed to download prediction data',
    description: "Error notice when a section row's Download Data fails and the failure carries no message.",
  },
  downloadPredictionFailedWithError: {
    id: 'documents.sections.download.predictionFailedWithError',
    defaultMessage: 'Failed to download prediction data: {error}',
    description: "Error notice when a section row's Download Data fails. {error} is the failure's message, in English.",
  },
  downloadBaselineFailed: {
    id: 'documents.sections.download.baselineFailed',
    defaultMessage: 'Failed to download baseline data',
    description: "Error notice when a section row's Download Baseline fails and the failure carries no message.",
  },
  downloadBaselineFailedWithError: {
    id: 'documents.sections.download.baselineFailedWithError',
    defaultMessage: 'Failed to download baseline data: {error}',
    description: "Error notice when a section row's Download Baseline fails. {error} is the failure's message, in English.",
  },
  downloadBaselineNotFound: {
    id: 'documents.sections.download.baselineNotFound',
    defaultMessage: 'Baseline data not found. The baseline may not have been set for this document yet.',
    description: "Error notice when a section row's Download Baseline finds no baseline to download.",
  },
  editData: {
    id: 'documents.sections.row.editData',
    defaultMessage: 'Edit Data',
    description:
      "Item of a section row's menu in the Document Sections table that opens the section's data on the edit page, for a role that can edit.",
  },
  viewData: {
    id: 'documents.sections.row.viewData',
    defaultMessage: 'View Data',
    description:
      "Item of a section row's menu in the Document Sections table that opens the section's data on the edit page, for a role that can't edit.",
  },
  downloadData: {
    id: 'documents.sections.row.downloadData',
    defaultMessage: 'Download Data',
    description: "Item of a section row's menu that downloads the section's data as a JSON file.",
  },
  downloadBaseline: {
    id: 'documents.sections.row.downloadBaseline',
    defaultMessage: 'Download Baseline',
    description:
      "Item of a section row's menu that downloads the section's evaluation baseline as a JSON file, offered when the document has one.",
  },
  actionsAriaLabel: {
    id: 'documents.sections.row.actionsAriaLabel',
    defaultMessage: 'Actions for section {section}',
    description: "Screen-reader label of a section row's menu. {section} is the section id.",
  },
  emptyTitle: {
    id: 'documents.sections.empty.title',
    defaultMessage: 'No sections',
    description: 'Heading of the sections table when the document has no section.',
  },
  emptyBody: {
    id: 'documents.sections.empty.body',
    defaultMessage: 'This document has no sections.',
    description: 'Text of the sections table when the document has no section.',
  },
});

/** The column headers, keyed by column id; the ⚙ preferences name the columns with the same messages. */
const columnMessages = defineMessages<{
  id: NoMessageValues;
  class: NoMessageValues;
  pageIds: NoMessageValues;
  confidenceAlerts: NoMessageValues;
  missingRequiredFields: NoMessageValues;
  status: NoMessageValues;
  actions: NoMessageValues;
}>({
  id: {
    id: 'documents.sections.column.id',
    defaultMessage: 'Section ID',
    description: 'Column header of the sections table: the section id.',
  },
  class: {
    id: 'documents.sections.column.class',
    defaultMessage: 'Class/Type',
    description: 'Column header of the sections table: the class the section was classified as.',
  },
  pageIds: {
    id: 'documents.sections.column.pageIds',
    defaultMessage: 'Page IDs',
    description: "Column header of the sections table: the numbers of the section's pages.",
  },
  confidenceAlerts: {
    id: 'documents.sections.column.confidenceAlerts',
    defaultMessage: 'Low Confidence Fields',
    description: "Column header of the sections table: how many of the section's fields scored below their confidence threshold.",
  },
  missingRequiredFields: {
    id: 'documents.sections.column.missingRequiredFields',
    defaultMessage: 'Missing Required Fields',
    description: 'Column header of the sections table: how many required fields extraction found no value for.',
  },
  status: {
    id: 'documents.sections.column.status',
    defaultMessage: 'Status',
    description: "Column header of the sections table: the section's status, from its processing issues.",
  },
  actions: {
    id: 'documents.sections.column.actions',
    defaultMessage: 'Actions',
    description: 'Column header of the sections table: the row menu.',
  },
});

/** The text in the UI's language; without `intl` (tests reading the column ids) the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

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
        <Badge color="grey">
          {item.ExclusionReason ? (
            <FormattedMessage {...messages.skipped} values={{ reason: item.ExclusionReason }} />
          ) : (
            <FormattedMessage {...messages.skippedNoReason} />
          )}
        </Badge>
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
  const intl = useIntl();
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
      header={intl.formatMessage(suspected ? messages.suspectedHeader : messages.detectedHeader)}
      content={
        <SpaceBetween size="xs">
          {suspected ? (
            <Box variant="p">
              {item.Class ? (
                <FormattedMessage {...messages.suspectedBody} values={{ count, className: item.Class }} />
              ) : (
                <FormattedMessage {...messages.suspectedBodyNoClass} values={{ count }} />
              )}
            </Box>
          ) : (
            <Box variant="p">
              {item.Class ? (
                <FormattedMessage {...messages.detectedBody} values={{ count, className: item.Class }} />
              ) : (
                <FormattedMessage {...messages.detectedBodyNoClass} values={{ count }} />
              )}
            </Box>
          )}
          <Box variant="small" color="text-body-secondary">
            <FormattedMessage {...(suspected ? messages.suspectedHint : messages.detectedHint)} />
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

/**
 * Required fields of this section's class that extraction left absent (#1527),
 * as a count beside the low-confidence count, with the field names on hover. An
 * absent field raises no confidence alert, so it is listed here instead.
 */
export const MissingRequiredFieldsCell = ({ item }: { item: SectionItem }): React.JSX.Element => {
  const intl = useIntl();
  const missing = getSectionMissingRequiredFields(item);
  if (missing.length === 0) {
    return <StatusIndicator type="success">0</StatusIndicator>;
  }
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="medium"
      triggerType="custom"
      header={intl.formatMessage(messages.missingRequiredHeader)}
      content={
        <SpaceBetween size="xxs">
          {missing.map((field) => (
            // Paths are unique within a section, so the path is the key.
            <Box key={field.attributeName || '(unnamed field)'} variant="code">
              {field.attributeName || <FormattedMessage {...messages.missingRequiredUnnamed} />}
            </Box>
          ))}
          <Box variant="small" color="text-body-secondary">
            <FormattedMessage {...messages.missingRequiredBody} />
          </Box>
        </SpaceBetween>
      }
    >
      <span style={{ cursor: 'pointer' }}>
        <StatusIndicator type="warning">{missing.length}</StatusIndicator>
      </span>
    </Popover>
  );
};

// Processing status cell: a worst-severity StatusIndicator over the section's
// structured ProcessingIssues, wrapped in a hover Popover listing each issue's
// message + root cause. Reuses the getSectionIssueStatus helper (mirrors the
// hitl-status-renderer + confidence-alerts-utils conventions).
const StatusCell = ({ item }: { item: SectionItem }): React.JSX.Element => {
  const intl = useIntl();
  const issues = item.ProcessingIssues || [];
  const { type, label } = getSectionIssueStatus(item, intl);

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
      header={intl.formatMessage(messages.issuesHeader)}
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
                  {issue.code ? issueCodeLabel(issue.code, intl) : issue.stage || intl.formatMessage(messages.issueFallback)}
                </StatusIndicator>
              </Box>
              <Box variant="p">{issue.message}</Box>
              {issue.rootCause && (
                <Box variant="small" color="text-body-secondary">
                  <FormattedMessage {...messages.rootCause} values={{ rootCause: issue.rootCause }} />
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
  const intl = useIntl();
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
  const handleDownload = async (type: 'prediction' | 'baseline') => {
    setIsDownloading(true);

    try {
      const fileUri = type === 'baseline' ? constructBaselineUri(item.OutputJSONUri) : item.OutputJSONUri;

      if (!fileUri) {
        setErrorMessage(intl.formatMessage(messages.downloadNoFileUri));
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

      // The failure's own message is shown as it is; which download failed picks the sentence.
      const failure = (error as Error).message;
      let errorMessage = intl.formatMessage(type === 'baseline' ? messages.downloadBaselineFailed : messages.downloadPredictionFailed);

      if (type === 'baseline' && failure?.includes('not found')) {
        errorMessage = intl.formatMessage(messages.downloadBaselineNotFound);
      } else if (failure) {
        errorMessage = intl.formatMessage(
          type === 'baseline' ? messages.downloadBaselineFailedWithError : messages.downloadPredictionFailedWithError,
          { error: failure },
        );
      }

      setErrorMessage(errorMessage);
    } finally {
      setIsDownloading(false);
    }
  };

  const items: ButtonDropdownProps.ItemOrGroup[] = [
    {
      id: 'edit-data',
      text: intl.formatMessage(canEdit ? messages.editData : messages.viewData),
      href: documentEditHref(objectKey, { section: String(item.Id), from: 'document' }),
      ...(canEdit ? { iconName: 'edit' as const } : {}),
      disabled: !objectKey || !item.OutputJSONUri,
      disabledReason: !item.OutputJSONUri ? intl.formatMessage(actionReasonMessages.sectionNoData) : undefined,
    },
    { id: 'prediction', text: intl.formatMessage(messages.downloadData), iconName: 'download' },
    ...(isBaselineAvailable(documentItem)
      ? [{ id: 'baseline', text: intl.formatMessage(messages.downloadBaseline), iconName: 'download' as const }]
      : []),
  ];

  return (
    <ButtonDropdown
      items={items}
      variant="inline-icon"
      expandToViewport
      ariaLabel={intl.formatMessage(messages.actionsAriaLabel, { section: String(item.Id) })}
      loading={isDownloading}
      onItemClick={({ detail }) => {
        if (detail.id === 'prediction' || detail.id === 'baseline') void handleDownload(detail.id);
      }}
    />
  );
};

/** The data columns a user may show or hide (⚙), in their order; each is named by its header's message. */
const SECTION_COLUMNS = ['id', 'class', 'pageIds', 'confidenceAlerts', 'missingRequiredFields', 'status'] as const;

/**
 * The sections table's columns, the ⋮ Actions column last. Their widths come from
 * fillTableColumns (common/detail-table-columns): Actions fixed on the right, the data
 * columns sharing the rest evenly.
 */
export const sectionColumnDefinitions = (
  documentItem: DocumentItem | undefined,
  mergedConfig: Record<string, unknown> | null | undefined,
  classificationIndex: ClassificationIndex,
  intl?: IntlShape,
) => {
  return [
    {
      id: 'id',
      header: labelOf(columnMessages.id, intl),
      cell: (item: SectionItem) => <IdCell item={item} />,
      sortingField: 'Id',
    },
    {
      id: 'class',
      header: labelOf(columnMessages.class, intl),
      cell: (item: SectionItem) => <ClassCell item={item} classificationIndex={classificationIndex} />,
      sortingField: 'Class',
    },
    {
      id: 'pageIds',
      header: labelOf(columnMessages.pageIds, intl),
      cell: (item: SectionItem) => <PageIdsCell item={item} />,
    },
    {
      id: 'confidenceAlerts',
      header: labelOf(columnMessages.confidenceAlerts, intl),
      cell: (item: SectionItem) => <ConfidenceAlertsCell item={item} mergedConfig={mergedConfig} />,
    },
    {
      id: 'missingRequiredFields',
      header: labelOf(columnMessages.missingRequiredFields, intl),
      cell: (item: SectionItem) => <MissingRequiredFieldsCell item={item} />,
    },
    {
      id: 'status',
      header: labelOf(columnMessages.status, intl),
      cell: (item: SectionItem) => <StatusCell item={item} />,
    },
    {
      id: 'actions',
      header: labelOf(columnMessages.actions, intl),
      cell: (item: SectionItem) => <ActionsCell item={item} documentItem={documentItem} />,
    },
  ];
};

const SectionsPanel = ({
  sections,
  documentItem,
  mergedConfig,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
}: SectionsPanelProps): React.JSX.Element => {
  const intl = useIntl();
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
  const columnOptions = SECTION_COLUMNS.map((id) => ({ id, label: intl.formatMessage(columnMessages[id]) }));
  const { preferences, setPreferences, visibleColumns } = useColumnPreferences('document-sections-columns', columnOptions);

  return (
    <Container
      header={
        <Header variant="h2">
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <div style={{ overflowX: 'auto', position: 'relative' }}>
        <Table
          columnDefinitions={fillTableColumns(
            sectionColumnDefinitions(documentItem, mergedConfig, classificationIndex, intl),
            visibleColumns,
          )}
          visibleColumns={visibleColumns}
          preferences={
            <ColumnPreferences
              title={intl.formatMessage(messages.title)}
              options={columnOptions}
              preferences={preferences}
              setPreferences={setPreferences}
            />
          }
          items={sortedItems}
          {...collectionProps}
          variant="embedded"
          stickyHeader={false}
          empty={
            <Box textAlign="center" color="inherit">
              <b>
                <FormattedMessage {...messages.emptyTitle} />
              </b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                <FormattedMessage {...messages.emptyBody} />
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
