// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  Alert,
  Box,
  ColumnLayout,
  Container,
  SpaceBetween,
  Header,
  ExpandableSection,
  StatusIndicator,
} from '@cloudscape-design/components';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { formatConfigVersionLink } from '../test-studio/utils/configVersionUtils';
import type { ConfigVersion } from '../test-studio/utils/configVersionUtils';
import { ConsoleLogger } from 'aws-amplify/utils';
import './DocumentPanel.css';
import SectionsPanel from '../sections-panel';
import PagesPanel from '../pages-panel';
import ChatPanel from '../chat-panel';
import useConfiguration from '../../hooks/use-configuration';
import usePricing from '../../hooks/use-pricing';
import useUserRole from '../../hooks/use-user-role';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { getDocumentConfidenceAlertCount } from '../common/confidence-alerts-utils';
import { renderHitlStatus } from '../common/hitl-status-renderer';
import { getMalwareScanView } from '../common/malware-scan';
import { renderObjectStatus } from '../common/object-status-renderer';
import DocumentVersionsPanel from './DocumentVersionsPanel';
import type { DocumentVersionDetail } from './DocumentVersionsPanel';
import { DocumentVersionProvider } from '../../contexts/document-version';
import usePolling from '../../hooks/use-polling';
import useClassificationComparison from '../../hooks/use-classification-comparison';
import { calculateTotalCosts } from './metering-cost';
import MeteringTable from './MeteringTable';
import type { PricingData } from './MeteringTable';
import type { PricingLookup } from './pricing';
import { isProfileReadable } from '../../utils/readable-profile';
import { lastSkipAllReason, normalizeHitlStatus } from '../common/review-status';
import { SLOW_POLL_STATUSES, TERMINAL_STATUSES } from '../common/document-statuses';
import ActionBar from '../common/ActionBar';
import DocumentPageHeader from '../common/DocumentPageHeader';
import FileViewer from '../document-viewer/FileViewer';
import EditorNotices from '../edit-screen/EditorNotices';
import { documentName } from '../review-queues/review-api';
import useDocumentActions from '../document-actions/use-document-actions';
import type { DocumentView } from '../document-actions/use-document-actions';
import useDocumentDownload from '../document-actions/use-document-download';
// Uncomment the line below to enable debugging

interface MappedDocument {
  objectKey: string;
  objectStatus: string;
  initialEventTime?: string;
  completionTime?: string;
  duration?: string;
  configVersion?: string;
  /** Where configVersion came from — see CONFIG_SOURCE_LABELS. */
  configSource?: string;
  /** The config prefix mapping that decided it, when one did. */
  configMappingPrefix?: string;
  /** Why the document was refused at ingest, for a `reject` prefix mapping. */
  configAssignmentError?: string;
  pageCount?: number;
  evaluationStatus?: string;
  evaluationReportUri?: string;
  summaryReportUri?: string;
  ruleValidationResultUri?: string;
  sections?: Record<string, unknown>[];
  pages?: Record<string, unknown>[];
  metering?: Record<string, Record<string, unknown>> | null;
  executionArn?: string;
  hitlStatus?: string;
  hitlTriggered?: boolean;
  hitlCompleted?: boolean;
  hitlReviewOwner?: string;
  hitlReviewOwnerEmail?: string;
  hitlReviewedBy?: string;
  hitlReviewedByEmail?: string;
  mergedConfig?: Record<string, unknown>;
  // Declared rather than left to the index signature below, so a typo in one of
  // these names is a type error here instead of a field that silently reads
  // `undefined` and renders nothing — the malware verdict failing to display is
  // indistinguishable from there being no verdict.
  malwareScanStatus?: string;
  malwareScanResult?: string;
  malwareScanThreat?: string;
  malwareScanReasons?: string[];
  malwareScanTime?: string;
  [key: string]: unknown;
}

interface ConfidenceAlertsSectionProps {
  sections: Record<string, unknown>[] | undefined;
  mergedConfig: Record<string, unknown> | undefined;
}

interface MeteringExpandableSectionProps {
  meteringData: Record<string, Record<string, unknown>> | null;
  documentItem: MappedDocument;
}

interface DocumentAttributesProps {
  item: MappedDocument;
  versions: ConfigVersion[];
  /** The Evaluation field: the status, or the baseline copy's state. */
  evaluationCell?: React.ReactNode;
}

interface DocumentPanelProps {
  item: MappedDocument;
  getDocumentDetailsFromIds?: (ids: string[]) => Promise<unknown>;
  onDelete?: (() => void) | null;
  onReprocess?: (() => void) | null;
  onAbort?: (() => void) | null;
}

/**
 * How each `ConfigSource` value reads to an operator. The raw values are the
 * wire/DB spelling (idp_common.config.prefix_mappings SOURCE_*); these are the
 * sentence fragments that answer "why this profile?".
 */
const CONFIG_SOURCE_LABELS: Record<string, string> = {
  'prefix-mapping': 'Assigned by prefix mapping',
  metadata: 'Specified at upload',
  'active-profile': 'Active profile',
  'document-pin': 'Kept from the previous run',
  'explicit-request': 'Chosen when reprocessing',
  'internal-producer': 'Pinned by the submitting feature',
  rejected: 'Refused at ingest',
};

const logger = new ConsoleLogger('DocumentPanel');

// Detail-view polling cadence under the HTTP API transport (no subscriptions).
const USE_POLLING = true;
const DOCUMENT_DETAIL_POLL_INTERVAL_MS = 4000;
// A document waiting on a reviewer changes on human time, not processing time.
const DOCUMENT_REVIEW_POLL_INTERVAL_MS = 30_000;

// Component to display confidence alerts count only
const ConfidenceAlertsSection = ({ sections, mergedConfig }: ConfidenceAlertsSectionProps): React.JSX.Element => {
  // Uncomment the line below to enable debugging
  // debugDocumentStructure({ sections, mergedConfig });

  if (!sections || !Array.isArray(sections) || !mergedConfig) {
    return <StatusIndicator type="success">0</StatusIndicator>;
  }

  const totalAlertCount = getDocumentConfidenceAlertCount(sections, mergedConfig);

  if (totalAlertCount === 0) {
    return <StatusIndicator type="success">0</StatusIndicator>;
  }

  return <StatusIndicator type="warning">{totalAlertCount}</StatusIndicator>;
};

// Expandable section containing the metering table
/** Estimated Cost, in its own container below Document Details. */
const MeteringExpandableSection = ({ meteringData, documentItem }: MeteringExpandableSectionProps): React.JSX.Element => {
  const [expanded, setExpanded] = useState(false);
  const { pricing } = usePricing();
  const [pricingData, setPricingData] = useState<PricingLookup | null>(null);

  // Convert pricing data to lookup format
  useEffect(() => {
    if (pricing && (pricing as PricingData).pricing) {
      const pricingLookup: PricingLookup = {};
      (pricing as PricingData).pricing.forEach((item) => {
        if (item.name && item.units) {
          pricingLookup[item.name] = {};
          item.units.forEach((unitItem) => {
            if (unitItem.name && unitItem.price !== undefined) {
              pricingLookup[item.name][unitItem.name] = Number(unitItem.price);
            }
          });
        }
      });
      setPricingData(pricingLookup);
    }
  }, [pricing]);

  // Calculate the cost per page for the header
  const { totalCost, costPerPage } = calculateTotalCosts(meteringData, documentItem?.pageCount, pricingData);

  return (
    <ExpandableSection
      variant="container"
      headerText={`Estimated Cost (per page: $${costPerPage.toFixed(4)})`}
      expanded={expanded}
      onChange={({ detail }) => setExpanded(detail.expanded)}
    >
      <div style={{ width: '100%' }}>
        <MeteringTable meteringData={meteringData} preCalculatedTotals={{ totalCost, costPerPage }} />
      </div>
    </ExpandableSection>
  );
};

const isHitlSkippedStatus = (raw: string | null | undefined): boolean => normalizeHitlStatus(raw).kind === 'skipped';

/** One field of the Document Details grid: its label over its value. */
const Field = ({ label, children }: { label: string; children: React.ReactNode }): React.JSX.Element => (
  <div>
    <Box variant="awsui-key-label">{label}</Box>
    <div>{children}</div>
  </div>
);

/**
 * Document Details: four aligned columns, fewer on a narrow screen (`minColumnWidth`).
 */
export const DocumentAttributes = ({ item, versions, evaluationCell }: DocumentAttributesProps): React.JSX.Element => {
  const skipAll = isHitlSkippedStatus(item.hitlStatus) ? lastSkipAllReason(item.hitlReviewHistory) : null;
  return (
    <ColumnLayout columns={4} minColumnWidth={160} variant="text-grid">
      <Field label="Document ID">{item.objectKey}</Field>
      <Field label="Status">{renderObjectStatus(item.objectStatus)}</Field>
      <Field label="Submitted">{item.initialEventTime}</Field>
      <Field label="Completed">{item.completionTime}</Field>
      <Field label="Duration">{item.duration}</Field>
      <Field label="Config Profile">
        {formatConfigVersionLink(item.configVersion, versions)}
        {/*
          WHERE that profile came from. There are several possible sources and
          the name alone does not say which applied, so a profile that
          surprises someone is otherwise unexplainable without correlating
          logs across two Lambdas.
        */}
        {item.configSource && (
          <Box fontSize="body-s" color="text-body-secondary">
            {CONFIG_SOURCE_LABELS[item.configSource] ?? item.configSource}
            {item.configMappingPrefix ? ` (${item.configMappingPrefix})` : ''}
          </Box>
        )}
        {item.configAssignmentError && (
          <Box fontSize="body-s" color="text-status-error">
            {item.configAssignmentError}
          </Box>
        )}
      </Field>
      <Field label="Page Count">{item.pageCount || 0}</Field>
      <Field label="Evaluation">{evaluationCell ?? (item.evaluationStatus || 'N/A')}</Field>
      <Field label="Review Status">{renderHitlStatus(item)}</Field>
      <Field label="Review Owner">{item.hitlReviewOwnerEmail || item.hitlReviewOwner || '-'}</Field>
      <Field label="Review Completed By">{item.hitlReviewedByEmail || item.hitlReviewedBy || '-'}</Field>
      {skipAll && (
        <Field label="Skip Reason">
          {skipAll.reason}
          {skipAll.note && (
            <Box variant="small" color="text-body-secondary">
              {skipAll.note}
            </Box>
          )}
        </Field>
      )}
      <Field label="Confidence Alerts">
        <ConfidenceAlertsSection sections={item.sections} mergedConfig={item.mergedConfig} />
      </Field>
      <Field label="Summary">{item.summaryReportUri ? 'Available' : 'N/A'}</Field>
      <MalwareScanAttribute item={item} />
    </ColumnLayout>
  );
};

/**
 * The malware-scan verdict as a detail field.
 *
 * Rendered for CLEAN documents too, not only refused ones: "the scan ran and
 * found nothing" is the observation an auditor needs, and showing it only on a
 * detection would make a clean document indistinguishable from an unscanned one.
 *
 * Renders nothing at all when the document carries no verdict and is not waiting
 * for one, which is every document on a stack with malware scanning disabled —
 * so enabling the feature is what makes this field appear, rather than it sitting
 * there reading N/A on every deployment.
 */
const MalwareScanAttribute = ({ item }: { item: MappedDocument }): React.JSX.Element | null => {
  const scan = getMalwareScanView(item);
  if (scan.outcome === 'none') return null;

  const statusType = {
    clean: 'success' as const,
    threat: 'error' as const,
    unscannable: 'warning' as const,
    pending: 'in-progress' as const,
  }[scan.outcome];

  return (
    <Field label="Malware scan">
      <StatusIndicator type={statusType}>{scan.label}</StatusIndicator>
      {scan.time ? (
        <Box fontSize="body-s" color="text-status-inactive">
          {new Date(scan.time).toLocaleString()}
        </Box>
      ) : null}
    </Field>
  );
};

/**
 * The whole story for a document the malware gate refused.
 *
 * This is the surface that replaces the flow viewer for such a document: it
 * never started an execution, so there is no execution to open and the diagram
 * would be empty. Without this the UI would show a terminal status nobody can
 * explain.
 */
const MalwareScanAlert = ({ item }: { item: MappedDocument }): React.JSX.Element | null => {
  const scan = getMalwareScanView(item);
  if (!scan.refused) return null;

  return (
    <Alert
      type={scan.outcome === 'threat' ? 'error' : 'warning'}
      header={scan.outcome === 'threat' ? 'Malware detected — document quarantined' : 'Document was not malware-scanned'}
    >
      {scan.detail}
    </Alert>
  );
};

export const DocumentPanel = ({
  item,
  getDocumentDetailsFromIds,
  onDelete,
  onReprocess,
  onAbort,
}: DocumentPanelProps): React.JSX.Element => {
  const { versions } = useConfigurationVersions();
  logger.debug('DocumentPanel item', item);

  const { settings } = useSettingsContext();
  const { currentCredentials, setErrorMessage } = useAppContext();
  // Local state for document item to enable real-time updates
  const [localItem, setLocalItem] = useState(item);
  // Details shows the page's widgets; Source shows the source document in their place.
  const [view, setView] = useState<DocumentView>('details');

  // Document version viewing: when set, the page renders a past run's snapshot
  // (read-only). versionFiles maps each output S3 key -> pinned VersionId so
  // viewers fetch that run's exact bytes.
  const [viewingRunId, setViewingRunId] = useState<string | null>(null);
  const [versionDetail, setVersionDetail] = useState<DocumentVersionDetail | null>(null);

  const handleViewVersion = useCallback((runId: string | null, detail: DocumentVersionDetail | null) => {
    setViewingRunId(runId);
    setVersionDetail(detail);
  }, []);

  // Update local item when prop changes
  useEffect(() => {
    setLocalItem(item);
  }, [item]);

  // If the underlying document changes (navigated to a different doc), drop any
  // historical-version view so we don't show a stale snapshot for the new doc.
  const itemObjectKey = item?.objectKey;
  useEffect(() => {
    setViewingRunId(null);
    setVersionDetail(null);
  }, [itemObjectKey]);

  // Detail-view polling (httpapi transport). Under AppSync the onUpdateDocument
  // subscription pushed live Sections/Pages updates into the open document.
  // With the HTTP API there are no subscriptions, so while the open document is
  // still processing we re-fetch its rich detail (getDocument via the parent's
  // getDocumentDetailsFromIds, which merges into shared list state and flows back
  // as the `item` prop). Stops once the document reaches a terminal status, unless a
  // copy to the evaluation baseline is still running, so its outcome shows.
  const detailStatus = (localItem?.objectStatus || '').toUpperCase();
  const isTerminalStatus = TERMINAL_STATUSES.includes(detailStatus);
  const keepPolling = !isTerminalStatus || localItem?.evaluationStatus === 'BASELINE_COPYING';
  const pollDetail = useCallback(() => {
    if (!localItem?.objectKey || !getDocumentDetailsFromIds) return;
    if (!keepPolling) return;
    void getDocumentDetailsFromIds([localItem.objectKey]);
  }, [localItem?.objectKey, keepPolling, getDocumentDetailsFromIds]);

  usePolling(pollDetail, {
    // Suspend live polling while viewing a historical version so the snapshot
    // isn't overwritten by a live re-fetch.
    enabled: USE_POLLING && !!localItem?.objectKey && keepPolling && viewingRunId === null,
    intervalMs: SLOW_POLL_STATUSES.includes(detailStatus) ? DOCUMENT_REVIEW_POLL_INTERVAL_MS : DOCUMENT_DETAIL_POLL_INTERVAL_MS,
  });

  // When the open document transitions to a terminal status (typically detected
  // by the lightweight list poll, which carries no Sections/Pages/Metering), the
  // detail poll above stops firing. Do one final rich getDocument fetch on that
  // transition so the completed sections, pages, and estimated cost are pulled in.
  const objectKey = localItem?.objectKey;
  const wasTerminalRef = useRef(isTerminalStatus);
  useEffect(() => {
    if (isTerminalStatus && !wasTerminalRef.current && objectKey && getDocumentDetailsFromIds) {
      void getDocumentDetailsFromIds([objectKey]);
    }
    wasTerminalRef.current = isTerminalStatus;
  }, [isTerminalStatus, objectKey, getDocumentDetailsFromIds]);

  const { allowedConfigVersions, loading: roleLoading } = useUserRole();
  // Fetch active configuration for dynamic confidence threshold (used by sections panel, etc.)
  const { mergedConfig, loading: defaultConfigLoading } = useConfiguration(
    isProfileReadable('default', versions, { allowedConfigVersions, loading: roleLoading }) ? 'default' : null,
  );
  // Fetch the specific configuration profile that was used to process this document (for flow viewer).
  const docConfigVersion = localItem?.configVersion || 'default';
  const needsSeparateVersionFetch = docConfigVersion !== 'default';
  const { mergedConfig: separateVersionConfig, loading: versionConfigLoading } = useConfiguration(
    needsSeparateVersionFetch ? docConfigVersion : null,
  );
  // Use the separate fetch result only when the version differs; otherwise reuse the default config
  const documentVersionConfig = needsSeparateVersionFetch ? separateVersionConfig : mergedConfig;
  const configLoading = !!(needsSeparateVersionFetch ? versionConfigLoading : defaultConfigLoading);

  // When viewing a past version, render that run's snapshot (sections, pages,
  // metering, report URIs) instead of the live document. The section/page
  // shapes from getDocumentVersion match what the sub-panels consume
  // (Id/Class/OutputJSONUri/ImageUri/...), and the DocumentVersionProvider
  // supplies the per-object VersionIds so file fetches read the run's bytes.
  const displayedItem = useMemo(() => {
    if (!viewingRunId || !versionDetail) return localItem;
    let parsedMetering = null;
    if (versionDetail.Metering) {
      try {
        parsedMetering = JSON.parse(versionDetail.Metering);
      } catch {
        parsedMetering = null;
      }
    }
    return {
      ...localItem,
      sections: (versionDetail.Sections ?? []) as Record<string, unknown>[],
      pages: (versionDetail.Pages ?? []) as Record<string, unknown>[],
      summaryReportUri: versionDetail.SummaryReportUri ?? '',
      evaluationReportUri: versionDetail.EvaluationReportUri ?? '',
      // Rule validation isn't versioned; hide it for historical views.
      ruleValidationResultUri: '',
      metering: parsedMetering,
    } as typeof localItem;
  }, [viewingRunId, versionDetail, localItem]);

  const refresh = useCallback(async () => {
    if (localItem?.objectKey && getDocumentDetailsFromIds) await getDocumentDetailsFromIds([localItem.objectKey]);
  }, [localItem?.objectKey, getDocumentDetailsFromIds]);

  const download = useDocumentDownload(localItem, settings, currentCredentials, setErrorMessage);
  const actions = useDocumentActions({
    item: localItem,
    displayed: displayedItem,
    isHistorical: viewingRunId !== null,
    mergedConfig: documentVersionConfig,
    configLoading,
    view,
    onViewChange: setView,
    refresh,
    onDocumentUpdate: (update) => setLocalItem((prev) => update(prev) as MappedDocument),
    download: download.menu,
    onReprocess: () => onReprocess?.(),
    onAbort: () => onAbort?.(),
    onDelete: () => onDelete?.(),
  });

  // Ground-truth-vs-predicted classification, loaded once here and shared by
  // the Sections and Pages tables so each annotates its Class/Type values from
  // one request. Empty for a document with no evaluation, in which case neither
  // table shows anything new.
  const classificationIndex = useClassificationComparison(displayedItem.evaluationReportUri);

  // Create enhanced item with configuration. Use the doc's own version
  // config so the header Confidence Alerts badge reads the threshold the
  // document was actually assessed against — otherwise the header count
  // can disagree with per-section alerts (which use documentVersionConfig
  // via SectionsPanel) whenever the stack's live threshold has drifted
  // from the version-at-processing-time threshold.
  const enhancedItem = {
    ...displayedItem,
    mergedConfig: documentVersionConfig ?? undefined,
  };

  return (
    <DocumentVersionProvider runId={viewingRunId} files={versionDetail?.Files}>
      <SpaceBetween size="l">
        {/* First on the page: the bar's test helpers read the first Header. */}
        <DocumentPageHeader
          title={documentName(localItem.objectKey)}
          status={localItem.objectStatus}
          objectKey={localItem.objectKey}
          actions={<ActionBar controls={actions.controls} />}
        />
        <EditorNotices notices={actions.notices} />
        {view === 'source' && <FileViewer objectKey={localItem.objectKey} />}
        {/* Hidden rather than unmounted behind Source, so Chat and the tables keep their state. */}
        <div data-testid="document-widgets" style={{ display: view === 'source' ? 'none' : undefined }}>
          <SpaceBetween size="l">
            <Container header={<Header variant="h2">Document Details</Header>}>
              <SpaceBetween size="l">
                {/* Above the attribute grid on purpose: for a refused document this
                    is the only explanation there is, since no workflow ran and the
                    flow viewer has no execution to show. */}
                <MalwareScanAlert item={enhancedItem} />

                <DocumentAttributes item={enhancedItem} versions={versions} evaluationCell={actions.evaluationStatusCell} />
              </SpaceBetween>
            </Container>
            {displayedItem.metering && <MeteringExpandableSection meteringData={displayedItem.metering} documentItem={displayedItem} />}
            <SectionsPanel
              sections={displayedItem.sections as never}
              documentItem={displayedItem}
              // The configuration profile the document was processed with, not the
              // stack's current live config: the classes it was classified against, and
              // its section confidence-alert thresholds. Falls back to `mergedConfig` when
              // the doc's version is 'default'/unset — see `documentVersionConfig` above.
              mergedConfig={documentVersionConfig}
              classificationIndex={classificationIndex}
            />
            <PagesPanel
              pages={displayedItem.pages as never}
              classificationIndex={classificationIndex}
              pageTextEdit={actions.pageTextEdit}
            />
            <DocumentVersionsPanel
              objectKey={localItem.objectKey}
              viewingRunId={viewingRunId}
              onViewVersion={handleViewVersion}
              completionTime={localItem.completionTime}
            />
            <ChatPanel objectKey={localItem.objectKey} configVersion={docConfigVersion} />
          </SpaceBetween>
        </div>

        {actions.dialogs}
        {download.dialogs}
      </SpaceBetween>
    </DocumentVersionProvider>
  );
};

export default DocumentPanel;
