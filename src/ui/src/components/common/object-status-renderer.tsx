// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { StatusIndicator } from '@cloudscape-design/components';
import type { StatusIndicatorProps } from '@cloudscape-design/components';

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
  'SUMMARIZING',
  'EVALUATING',
]);

/** One StatusIndicator for a tracking row's ObjectStatus. */
export const renderObjectStatus = (status: string | null | undefined): React.JSX.Element => {
  const raw = status ?? '';
  const view = VIEWS[raw];
  if (view) return <StatusIndicator type={view.type}>{raw}</StatusIndicator>;
  if (IN_FLIGHT.has(raw)) return <StatusIndicator type="in-progress">{raw}</StatusIndicator>;
  return <StatusIndicator type="info">{raw || 'Unknown'}</StatusIndicator>;
};

export default renderObjectStatus;
