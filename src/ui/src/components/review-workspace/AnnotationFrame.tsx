// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The frame of the annotation screen, the same for production and Test Studio items:
// the title row with the item's facts, the action bar (History, where the item has one;
// then Claim for an Annotator viewing an unassigned item, otherwise Skip, which gives
// the item back to its queue, and Complete), notices, the task context and the editor.
// Every disabled action says why on hover.
import React from 'react';
import { Button, Header, SpaceBetween } from '@cloudscape-design/components';

import ReviewWorkspaceLayout from './ReviewWorkspaceLayout';

export type AnnotationAction = 'complete' | 'skip' | 'claim';

/** Hover text of Skip while it is available. */
export const SKIP_HINT = 'Give this item back to its queue, unassigned and not finished.';

export interface AnnotationActionsProps {
  /** Why Complete is unavailable; undefined when it is available. */
  completeDisabledReason?: string;
  /** Hover text while Complete is available. */
  completeHint?: string;
  /** Offer Skip: give the item back to its queue, unassigned and not finished. */
  showSkip?: boolean;
  skipDisabledReason?: string;
  /**
   * Offer Claim (an Annotator viewing an unassigned item): assigns the item to them,
   * after which it can be worked. Shown instead of the other actions.
   */
  showClaim?: boolean;
  /** Offer History: open the item's review history. Shown first, whatever the state. */
  onShowHistory?: () => void;
  busy: AnnotationAction | null;
  onAction: (action: AnnotationAction) => void;
}

const HistoryButton = ({ onShowHistory }: { onShowHistory?: () => void }): React.JSX.Element | null =>
  onShowHistory ? (
    <span title="Who claimed, skipped or completed this item, and why">
      <Button iconName="status-info" onClick={onShowHistory}>
        History
      </Button>
    </span>
  ) : null;

const AnnotationActions = ({
  completeDisabledReason,
  completeHint,
  showSkip = false,
  skipDisabledReason,
  showClaim = false,
  onShowHistory,
  busy,
  onAction,
}: AnnotationActionsProps): React.JSX.Element => {
  const working = busy !== null ? 'Working…' : undefined;
  if (showClaim) {
    return (
      <SpaceBetween direction="horizontal" size="xs">
        <HistoryButton onShowHistory={onShowHistory} />
        <span title={working ? undefined : 'Assign this item to you, then work it here or from My Work › Tasks'}>
          <Button variant="primary" disabled={!!working && busy !== 'claim'} loading={busy === 'claim'} onClick={() => onAction('claim')}>
            Claim
          </Button>
        </span>
      </SpaceBetween>
    );
  }
  const completeReason = working ?? completeDisabledReason;
  const skipReason = working ?? skipDisabledReason;
  return (
    <SpaceBetween direction="horizontal" size="xs">
      <HistoryButton onShowHistory={onShowHistory} />
      {showSkip && (
        <span title={skipReason ? undefined : SKIP_HINT}>
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
  /** Beside the title: status and kind badge. */
  badges?: React.ReactNode;
  /** Under the title: where the item comes from and who it is assigned to. */
  meta?: React.ReactNode;
  actions: AnnotationActionsProps;
  /** Alerts about the item's state. */
  notices?: React.ReactNode;
  /** The task context (why the item is here, the version transition). */
  context?: React.ReactNode;
  /** Dialogs the actions open (the skip reason, the review history). */
  dialogs?: React.ReactNode;
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
  dialogs,
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
    {dialogs}
  </ReviewWorkspaceLayout>
);

export default AnnotationFrame;
