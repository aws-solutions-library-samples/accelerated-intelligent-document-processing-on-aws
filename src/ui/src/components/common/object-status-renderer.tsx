// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { StatusIndicator } from '@cloudscape-design/components';
import type { StatusIndicatorProps } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

// The Status column shows each stored value as it is, so it reads the same as the CLI,
// the SDK and IDP Monitor, which report the same values.
const VIEWS: Record<string, { type: StatusIndicatorProps.Type }> = {
  COMPLETED: { type: 'success' },
  FAILED: { type: 'error' },
  ABORTED: { type: 'stopped' },
  HITL_IN_PROGRESS: { type: 'in-progress' },
  REJECTED: { type: 'stopped' },
  REDACTED_SUPERSEDED: { type: 'info' },
  QUEUED: { type: 'pending' },
  // Refused at the ingest malware gate (see ../common/malware-scan).
  QUARANTINED: { type: 'error' },
  MALWARE_SCAN_FAILED: { type: 'warning' },
  MALWARE_SCANNING: { type: 'in-progress' },
};

const IN_FLIGHT = new Set([
  'RUNNING',
  'PREPROCESSING',
  'OCR',
  'CLASSIFYING',
  'EXTRACTING',
  'ASSESSING',
  'POSTPROCESSING',
  // Policy rule validation: classifying the policy, validating the rules, consolidating.
  'RULE_VALIDATION_POLICY_CLASSIFICATION',
  'RULE_VALIDATION',
  'RULE_VALIDATION_ORCHESTRATOR',
  'SUMMARIZING',
  'EVALUATING',
]);

/**
 * The members of the backend `Status` enum (`idp_common.models`). The Python test
 * `scripts/tests/test_document_status_ui_parity.py` reads both lists from source and fails
 * when they differ, in either direction.
 */
type StatusValue =
  | 'PENDING_UPLOAD'
  | 'IN_PROGRESS'
  | 'MALWARE_SCANNING'
  | 'QUEUED'
  | 'RUNNING'
  | 'PREPROCESSING'
  | 'OCR'
  | 'CLASSIFYING'
  | 'EXTRACTING'
  | 'ASSESSING'
  | 'POSTPROCESSING'
  | 'HITL_IN_PROGRESS'
  | 'SUMMARIZING'
  | 'RULE_VALIDATION_POLICY_CLASSIFICATION'
  | 'RULE_VALIDATION'
  | 'RULE_VALIDATION_ORCHESTRATOR'
  | 'EVALUATING'
  | 'COMPLETED'
  | 'FAILED'
  | 'ABORTED'
  | 'REDACTED_SUPERSEDED'
  | 'REJECTED'
  | 'QUARANTINED'
  | 'MALWARE_SCAN_FAILED';

// One message per stored value, whose English IS the stored value (see the note above
// VIEWS); a translation may spell it out. Enum values only: the parity test reads the keys.
const statusMessages = defineMessages<Record<StatusValue, NoMessageValues>>({
  PENDING_UPLOAD: {
    id: 'documents.status.pendingUpload',
    defaultMessage: 'PENDING_UPLOAD',
    description:
      'Document status PENDING_UPLOAD as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  IN_PROGRESS: {
    id: 'documents.status.inProgress',
    defaultMessage: 'IN_PROGRESS',
    description:
      'Document status IN_PROGRESS as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  MALWARE_SCANNING: {
    id: 'documents.status.malwareScanning',
    defaultMessage: 'MALWARE_SCANNING',
    description:
      'Document status MALWARE_SCANNING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  QUEUED: {
    id: 'documents.status.queued',
    defaultMessage: 'QUEUED',
    description:
      'Document status QUEUED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  RUNNING: {
    id: 'documents.status.running',
    defaultMessage: 'RUNNING',
    description:
      'Document status RUNNING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  PREPROCESSING: {
    id: 'documents.status.preprocessing',
    defaultMessage: 'PREPROCESSING',
    description:
      'Document status PREPROCESSING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  OCR: {
    id: 'documents.status.ocr',
    defaultMessage: 'OCR',
    description:
      'Document status OCR as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  CLASSIFYING: {
    id: 'documents.status.classifying',
    defaultMessage: 'CLASSIFYING',
    description:
      'Document status CLASSIFYING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  EXTRACTING: {
    id: 'documents.status.extracting',
    defaultMessage: 'EXTRACTING',
    description:
      'Document status EXTRACTING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  ASSESSING: {
    id: 'documents.status.assessing',
    defaultMessage: 'ASSESSING',
    description:
      'Document status ASSESSING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  POSTPROCESSING: {
    id: 'documents.status.postprocessing',
    defaultMessage: 'POSTPROCESSING',
    description:
      'Document status POSTPROCESSING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  HITL_IN_PROGRESS: {
    id: 'documents.status.hitlInProgress',
    defaultMessage: 'HITL_IN_PROGRESS',
    description:
      'Document status HITL_IN_PROGRESS as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  SUMMARIZING: {
    id: 'documents.status.summarizing',
    defaultMessage: 'SUMMARIZING',
    description:
      'Document status SUMMARIZING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  RULE_VALIDATION_POLICY_CLASSIFICATION: {
    id: 'documents.status.ruleValidationPolicyClassification',
    defaultMessage: 'RULE_VALIDATION_POLICY_CLASSIFICATION',
    description:
      'Document status RULE_VALIDATION_POLICY_CLASSIFICATION as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  RULE_VALIDATION: {
    id: 'documents.status.ruleValidation',
    defaultMessage: 'RULE_VALIDATION',
    description:
      'Document status RULE_VALIDATION as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  RULE_VALIDATION_ORCHESTRATOR: {
    id: 'documents.status.ruleValidationOrchestrator',
    defaultMessage: 'RULE_VALIDATION_ORCHESTRATOR',
    description:
      'Document status RULE_VALIDATION_ORCHESTRATOR as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  EVALUATING: {
    id: 'documents.status.evaluating',
    defaultMessage: 'EVALUATING',
    description:
      'Document status EVALUATING as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  COMPLETED: {
    id: 'documents.status.completed',
    defaultMessage: 'COMPLETED',
    description:
      'Document status COMPLETED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  FAILED: {
    id: 'documents.status.failed',
    defaultMessage: 'FAILED',
    description:
      'Document status FAILED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  ABORTED: {
    id: 'documents.status.aborted',
    defaultMessage: 'ABORTED',
    description:
      'Document status ABORTED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  REDACTED_SUPERSEDED: {
    id: 'documents.status.redactedSuperseded',
    defaultMessage: 'REDACTED_SUPERSEDED',
    description:
      'Document status REDACTED_SUPERSEDED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  REJECTED: {
    id: 'documents.status.rejected',
    defaultMessage: 'REJECTED',
    description:
      'Document status REJECTED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  QUARANTINED: {
    id: 'documents.status.quarantined',
    defaultMessage: 'QUARANTINED',
    description:
      'Document status QUARANTINED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
  MALWARE_SCAN_FAILED: {
    id: 'documents.status.malwareScanFailed',
    defaultMessage: 'MALWARE_SCAN_FAILED',
    description:
      'Document status MALWARE_SCAN_FAILED as stored. English shows the stored value so the UI reads like the CLI, the SDK and IDP Monitor; a translation may spell it out.',
  },
});

const fallbackMessages = defineMessages<{ unknown: NoMessageValues }>({
  unknown: {
    id: 'documents.status.unknown',
    defaultMessage: 'Unknown',
    description: 'Status column text for a document row whose stored status is empty.',
  },
});

const isStatusValue = (value: string): value is StatusValue => Object.hasOwn(statusMessages, value);

/** The label in the UI's language; without `intl`, the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/**
 * The text the Status column shows for a stored status: a known value's message (the value
 * itself in English), a value the UI does not know as it is, an empty one as Unknown.
 */
export const statusLabel = (status: string | null | undefined, intl?: IntlShape): string => {
  const raw = status ?? '';
  if (isStatusValue(raw)) return labelOf(statusMessages[raw], intl);
  return raw || labelOf(fallbackMessages.unknown, intl);
};

/** One StatusIndicator for a tracking row's ObjectStatus. */
export const renderObjectStatus = (status: string | null | undefined, intl?: IntlShape): React.JSX.Element => {
  const raw = status ?? '';
  const label = statusLabel(status, intl);
  const view = VIEWS[raw];
  if (view) return <StatusIndicator type={view.type}>{label}</StatusIndicator>;
  if (IN_FLIGHT.has(raw)) return <StatusIndicator type="in-progress">{label}</StatusIndicator>;
  return <StatusIndicator type="info">{label}</StatusIndicator>;
};

export default renderObjectStatus;
