// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// How an Inbox message severity is shown: one mapping for the bell, the table and
// the message modal.
import type { IconProps, StatusIndicatorProps } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

import type { InboxSeverity } from '../../contexts/inbox';

/**
 * One label per severity the backend writes (`Severity` in idp_common/notifications/models.py);
 * scripts/tests/test_inbox_severity_ui_parity.py checks the two lists match.
 */
const severityMessages = defineMessages<Record<InboxSeverity, NoMessageValues>>({
  info: {
    id: 'inbox.severity.info',
    defaultMessage: 'Info',
    description: 'Inbox message severity: for information. Shown in the Inbox table, its filter, the bell and the message dialog.',
  },
  success: {
    id: 'inbox.severity.success',
    defaultMessage: 'Success',
    description: 'Inbox message severity: something succeeded. Shown in the Inbox table, its filter, the bell and the message dialog.',
  },
  warning: {
    id: 'inbox.severity.warning',
    defaultMessage: 'Warning',
    description:
      'Inbox message severity: something needs attention. Shown in the Inbox table, its filter, the bell and the message dialog.',
  },
  error: {
    id: 'inbox.severity.error',
    defaultMessage: 'Error',
    description: 'Inbox message severity: something failed. Shown in the Inbox table, its filter, the bell and the message dialog.',
  },
});

/** Every severity the backend writes, in the order the Inbox table's filter offers them. */
export const INBOX_SEVERITIES = Object.keys(severityMessages) as readonly InboxSeverity[];

const isKnownSeverity = (severity: string): severity is InboxSeverity => Object.hasOwn(severityMessages, severity);

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

/**
 * A severity's label in the UI's language; without `intl`, the English source text. A value
 * the backend does not write yet is shown capitalised, as stored.
 */
export const severityLabel = (severity: string, intl?: IntlShape): string => {
  if (!isKnownSeverity(severity)) return severity ? severity.charAt(0).toUpperCase() + severity.slice(1) : '';
  const message = severityMessages[severity];
  return intl ? intl.formatMessage(message) : String(message.defaultMessage);
};
