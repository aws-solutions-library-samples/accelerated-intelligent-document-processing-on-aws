// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// How an Inbox message severity is shown: one mapping for the bell, the table and
// the message modal.
import type { IconProps, StatusIndicatorProps } from '@cloudscape-design/components';

export const severityStatusType = (severity: string): StatusIndicatorProps.Type => {
  switch (severity) {
    case 'success':
    case 'warning':
    case 'error':
      return severity;
    default:
      return 'info';
  }
};

export const severityIcon = (severity: string): IconProps.Name => {
  switch (severityStatusType(severity)) {
    case 'success':
      return 'status-positive';
    case 'warning':
      return 'status-warning';
    case 'error':
      return 'status-negative';
    default:
      return 'status-info';
  }
};

// Sort order, least to most severe; unknown severities sort with info.
const SEVERITY_RANK: Record<string, number> = { success: 0, info: 1, warning: 2, error: 3 };

export const severityRank = (severity: string): number => SEVERITY_RANK[severity] ?? SEVERITY_RANK.info;

export const severityLabel = (severity: string): string => (severity ? severity.charAt(0).toUpperCase() + severity.slice(1) : '');
