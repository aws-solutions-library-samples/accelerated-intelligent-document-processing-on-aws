// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The frame of the annotation screen, the same for production and Test Studio items:
// the title row with the item's facts, the action bar (Complete, plus Reject for
// production and Skip for Test Studio), notices, the task context and the editor. Every disabled action says
// why on hover.
import React from 'react';
import { Button, Header, SpaceBetween } from '@cloudscape-design/components';

import ReviewWorkspaceLayout from './ReviewWorkspaceLayout';

export type AnnotationAction = 'complete' | 'reject' | 'skip';

export interface AnnotationActionsProps {
  /** Why Complete is unavailable; undefined when it is available. */
  completeDisabledReason?: string;
  /** Hover text while Complete is available. */
  completeHint?: string;
  /** Offer Reject (production items only). */
  showReject: boolean;
  rejectDisabledReason?: string;
  /** Offer Skip: complete the task without reviewing (Test Studio items). */
  showSkip?: boolean;
  skipDisabledReason?: string;
  busy: AnnotationAction | null;
  onAction: (action: AnnotationAction) => void;
}

const AnnotationActions = ({
  completeDisabledReason,
  completeHint,
  showReject,
  rejectDisabledReason,
  showSkip = false,
  skipDisabledReason,
  busy,
  onAction,
}: AnnotationActionsProps): React.JSX.Element => {
  const working = busy !== null ? 'Working…' : undefined;
  const completeReason = working ?? completeDisabledReason;
  const rejectReason = working ?? rejectDisabledReason;
  const skipReason = working ?? skipDisabledReason;
  return (
    <SpaceBetween direction="horizontal" size="xs">
      {showReject && (
        <Button disabled={!!rejectReason} disabledReason={rejectReason} loading={busy === 'reject'} onClick={() => onAction('reject')}>
          Reject
        </Button>
      )}
      {showSkip && (
        <span title={skipReason ? undefined : 'Complete the task without reviewing the labels'}>
          <Button disabled={!!skipReason} disabledReason={skipReason} loading={busy === 'skip'} onClick={() => onAction('skip')}>
            Skip
          </Button>
        </span>
      )}
      <span title={completeReason ? undefined : completeHint}>
        <Button
          variant="primary"
          disabled={!!completeReason}
          disabledReason={completeReason}
          loading={busy === 'complete'}
          onClick={() => onAction('complete')}
        >
          Complete
        </Button>
      </span>
    </SpaceBetween>
  );
};

interface AnnotationFrameProps {
  /** The document's name. */
  title: string;
  /** The full object key, shown under the title. */
  objectKey: string;
  /** Beside the title: status, kind badge, favorite. */
  badges?: React.ReactNode;
  /** Under the title: where the item comes from and who it is assigned to. */
  meta?: React.ReactNode;
  actions: AnnotationActionsProps;
  /** Alerts about the item's state. */
  notices?: React.ReactNode;
  /** The task context (why the item is here, the checklist, the version transition). */
  context?: React.ReactNode;
  /** The editor. */
  children: React.ReactNode;
}

const AnnotationFrame = ({
  title,
  objectKey,
  badges,
  meta,
  actions,
  notices,
  context,
  children,
}: AnnotationFrameProps): React.JSX.Element => (
  <ReviewWorkspaceLayout
    header={
      <SpaceBetween size="s">
        <Header variant="h1" description={<span title={objectKey}>{objectKey}</span>} actions={<AnnotationActions {...actions} />}>
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <span>{title}</span>
            {badges}
          </SpaceBetween>
        </Header>
        {meta}
        {notices}
      </SpaceBetween>
    }
    context={context}
  >
    {children}
  </ReviewWorkspaceLayout>
);

export default AnnotationFrame;
