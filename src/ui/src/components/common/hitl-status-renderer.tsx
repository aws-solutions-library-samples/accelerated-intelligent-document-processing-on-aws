// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { StatusIndicator } from '@cloudscape-design/components';

import { normalizeHitlStatus } from './review-status';

interface HitlItem {
  hitlTriggered?: boolean;
  hitlStatus?: string;
  hitlCompleted?: boolean;
}

export const renderHitlStatus = (item: HitlItem): React.JSX.Element => {
  const view = normalizeHitlStatus(item.hitlStatus);

  // Queue-managed documents carry their status whether or not the legacy flag is set.
  if (view.queueManaged) {
    if (view.kind === 'in_review') return <StatusIndicator type="in-progress">In review</StatusIndicator>;
    if (view.kind === 'complete') return <StatusIndicator type="success">Review complete</StatusIndicator>;
    return <StatusIndicator type="stopped">Rejected</StatusIndicator>;
  }

  if (!item.hitlTriggered) {
    return <StatusIndicator type="stopped">N/A</StatusIndicator>;
  }

  const status = item.hitlStatus?.toLowerCase().replace(/\s+/g, '') || '';

  if (status === 'failed' || status === 'reviewfailed') {
    return <StatusIndicator type="error">Review Failed</StatusIndicator>;
  }
  if (view.kind === 'skipped') {
    return <StatusIndicator type="stopped">Review Skipped</StatusIndicator>;
  }
  if (item.hitlCompleted || view.kind === 'complete') {
    return <StatusIndicator type="success">Review Completed</StatusIndicator>;
  }
  if (view.kind === 'in_progress') {
    return <StatusIndicator type="in-progress">Review In Progress</StatusIndicator>;
  }

  // HITL triggered but not completed - pending review
  return <StatusIndicator type="pending">Review Pending</StatusIndicator>;
};

export default renderHitlStatus;
